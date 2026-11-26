"""Read existing ProjectFlow records without copying logs, commands or user content."""

from datetime import datetime, timezone


def records(db, decision, view):
    if view["node"]:
        return []
    project = view["project"]
    end = datetime.fromtimestamp(view["at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    start = datetime.fromtimestamp(view["at"] - 86399, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if view["when"]:
        from app.services.fleet_steward import VN

        start = (
            datetime.strptime(view["when"], "%Y-%m-%d")
            .replace(tzinfo=VN)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    if view["mode"] == "history":
        return db.all(
            """
            SELECT * FROM (
              SELECT 'deploy' AS kind, d.id AS id, d.status AS value, d.source_commit AS version,
                d.environment AS target, COALESCE(d.finished_at,d.started_at,d.requested_at) AS stamp,
                r.github_owner_repo AS project FROM deployments d JOIN repositories r ON r.id=d.repository_id
              UNION ALL
              SELECT 'merge' AS kind,m.id AS id,m.merge_status AS value,m.merged_commit AS version,
                '' AS target,m.merged_at AS stamp,r.github_owner_repo AS project
                FROM merge_records m JOIN repositories r ON r.id=m.repository_id WHERE m.merged_at IS NOT NULL
            ) WHERE (?='' OR project=?) AND stamp<=? AND (?='' OR stamp>=?)
            ORDER BY stamp DESC LIMIT 100""",
            (project, project, end, view["when"], start),
        )
    if view["mode"] != "next" or view["when"]:
        return []
    tasks = db.all(
        """SELECT t.id FROM tasks t LEFT JOIN repositories r ON r.id=t.repo_scope_id
        WHERE t.closed_at IS NULL AND t.status!='CANCELLED' AND (?='' OR r.github_owner_repo=?)
        ORDER BY t.id DESC LIMIT 12""",
        (project, project),
    )
    result = []
    for task in tasks:
        state = decision.evaluate(task["id"])
        step = state.get("next_action") or {}
        if state["status"] != "DONE":
            result.append(
                dict(
                    kind="task",
                    id=task["id"],
                    value=step.get("label") or step.get("action") or state["stage"],
                    version="",
                    target=state["stage"],
                    stamp="",
                )
            )
    return result
