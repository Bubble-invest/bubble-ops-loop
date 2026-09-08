import json
from console.services.recent_messages import fetch_recent_messages

class Response:
    def __init__(self, payload): self.payload=payload
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def read(self): return json.dumps(self.payload).encode()

def prop(kind, value):
    if kind in ("title","rich_text"): return {"type":kind,kind:[{"plain_text":value}]}
    if kind in ("select","status"): return {"type":kind,kind:{"name":value}}
    if kind=="date": return {"type":"date","date":{"start":value}}

def test_fetch_recent_messages_is_fixed_read_only_projection():
    seen={}
    def opener(request, timeout):
        seen.update(url=request.full_url, method=request.method,
                    auth=request.headers.get("Authorization"), timeout=timeout,
                    body=json.loads(request.data))
        return Response({"results":[{"properties":{
            "Message":prop("title","Synthetic message"), "Type":prop("select","Email"),
            "Statut":prop("select","Envoyé"), "Date envoi":prop("date","2026-09-08"),
            "Envoyé sur le compte":prop("rich_text","Synthetic account"),
            "Validé par":prop("rich_text","Synthetic validator"),
            "Contenu":prop("rich_text","PRIVATE BODY MUST NOT PROJECT"),
        }}]})
    rows,error=fetch_recent_messages(api_key="secret",opener=opener,database_id="db")
    assert error is None and len(rows)==1
    assert "PRIVATE" not in repr(rows)
    assert seen["method"]=="POST" and seen["url"].endswith("/databases/db/query")
    assert "secret" not in seen["url"] and seen["auth"]=="Bearer secret"
    assert seen["body"]=={"page_size":15,"sorts":[{"property":"Date envoi","direction":"descending"}]}

def test_missing_key_fails_closed_without_api_call():
    rows,error=fetch_recent_messages(api_key="",opener=lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert rows==[] and error=="Messages indisponibles"
