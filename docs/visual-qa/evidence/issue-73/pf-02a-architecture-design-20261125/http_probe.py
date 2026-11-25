from fastapi.testclient import TestClient
_original_post = TestClient.post

def _post(self, url, *args, **kwargs):
    response = _original_post(self, url, *args, **kwargs)
    if any(part in str(url) for part in ("/architecture/analyze", "/design/technical", "/design/ui-ux", "/design/review")):
        content_type = response.headers.get("content-type", "")
        body = response.text[:220].replace("\n", " ")
        print(f"HTTP_PROBE method=POST path={url} status={response.status_code} content_type={content_type!r} body={body!r}", flush=True)
    return response

TestClient.post = _post
