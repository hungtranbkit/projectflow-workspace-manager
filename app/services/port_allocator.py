from __future__ import annotations
import socket


class PortAllocationError(RuntimeError): pass


class PortAllocatorService:
    """Allocates a host port per (sandbox, service) inside a contract-declared
    range. Persists the reservation so the SAME port survives stop/start;
    only released on sandbox cleanup. Checks both the DB (other sandboxes'
    live reservations) and the actual OS listener state, so a port a
    completely unrelated process is using is never handed out."""

    def __init__(self, db):
        self.db = db

    def _reserved_ports(self) -> set[int]:
        rows = self.db.all("SELECT host_port FROM sandbox_ports WHERE released_at IS NULL")
        return {r["host_port"] for r in rows}

    def _is_free(self, port: int) -> bool:
        for family in (socket.AF_INET,):
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    return False
        return True

    def allocate(self, sandbox_id: int, service: str, container_port: int, port_range: tuple[int, int]) -> int:
        existing = self.db.one(
            "SELECT host_port FROM sandbox_ports WHERE sandbox_id=? AND service=? AND released_at IS NULL",
            (sandbox_id, service),
        )
        if existing:
            return existing["host_port"]
        lo, hi = port_range
        reserved = self._reserved_ports()
        for port in range(lo, hi + 1):
            if port in reserved: continue
            if not self._is_free(port): continue
            self.db.execute(
                "INSERT INTO sandbox_ports(sandbox_id,service,host_port,container_port) VALUES(?,?,?,?)",
                (sandbox_id, service, port, container_port),
            )
            return port
        raise PortAllocationError(f"No free port for service '{service}' in range {lo}-{hi}")

    def release(self, sandbox_id: int) -> None:
        self.db.execute(
            "UPDATE sandbox_ports SET released_at=CURRENT_TIMESTAMP WHERE sandbox_id=? AND released_at IS NULL",
            (sandbox_id,),
        )

    def ports_for(self, sandbox_id: int) -> list[dict]:
        return self.db.all(
            "SELECT service,host_port,container_port FROM sandbox_ports WHERE sandbox_id=? AND released_at IS NULL",
            (sandbox_id,),
        )
