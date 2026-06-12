import requests
import azure.functions as func

UPSTREAM = "https://teams.microsoft.com/api/mt"

def main(req: func.HttpRequest) -> func.HttpResponse:
    path = req.route_params.get("path", "")
    url = f"{UPSTREAM}/{path}"

    # Forward all headers except Host
    headers = {k: v for k, v in req.headers.items()
               if k.lower() not in ("host", "content-length")}

    try:
        resp = requests.request(
            method=req.method,
            url=url,
            headers=headers,
            data=req.get_body(),
            timeout=30,
        )
    except requests.RequestException as e:
        return func.HttpResponse(str(e), status_code=502)

    # Strip transfer-encoding (Azure Functions handles its own)
    resp_headers = {k: v for k, v in resp.headers.items()
                    if k.lower() not in ("transfer-encoding", "content-encoding")}

    return func.HttpResponse(
        body=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
    )
