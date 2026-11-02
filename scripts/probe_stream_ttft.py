import json, os, time
import httpx
from dotenv import load_dotenv

load_dotenv(os.environ["ONCALL_ENV_FILE"])
key = os.environ["DEEPSEEK_API_KEY"]
base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
model = os.environ.get("LLM_MODEL", "deepseek-flash")
hdrs = {"Authorization": "Bearer " + key}


def run(name, extra, stream=True):
    body = {"model": model, "messages": [{"role": "user", "content": "reply with the single word: ok"}], "temperature": 0.2, "max_tokens": 20}
    body.update(extra)
    started = time.perf_counter()
    first_content = None
    first_reasoning = None
    content = []
    reasoning = []
    try:
        with httpx.Client(timeout=120) as c:
            if stream:
                body["stream"] = True
                with c.stream("POST", base + "/chat/completions", json=body, headers=hdrs) as r:
                    if r.status_code != 200:
                        r.read()
                        print("%-38s HTTP %s %s" % (name, r.status_code, r.text[:160]))
                        return
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        p = line[5:].strip()
                        if p == "[DONE]":
                            break
                        d = (json.loads(p).get("choices") or [{}])[0].get("delta", {})
                        rc = d.get("reasoning_content")
                        ct = d.get("content")
                        if rc:
                            if first_reasoning is None:
                                first_reasoning = time.perf_counter() - started
                            reasoning.append(rc)
                        if ct:
                            if first_content is None:
                                first_content = time.perf_counter() - started
                            content.append(ct)
            else:
                r = c.post(base + "/chat/completions", json=body, headers=hdrs)
                obj = r.json()
                if r.status_code != 200:
                    print("%-38s HTTP %s %s" % (name, r.status_code, str(obj)[:160]))
                    return
                msg = obj["choices"][0]["message"]
                content = [msg.get("content") or ""]
                reasoning = [msg.get("reasoning_content") or ""]
        print("%-38s first_reason=%-7s first_content=%-7s total=%-6s chars=%d/%d %s" % (
            name,
            round(first_reasoning, 3) if first_reasoning else "-",
            round(first_content, 3) if first_content else "-",
            round(time.perf_counter() - started, 3),
            sum(len(x) for x in reasoning),
            sum(len(x) for x in content),
            "".join(content)[:40].replace("\n", " "),
        ))
    except Exception as exc:
        print("%-38s EXC %s: %s" % (name, type(exc).__name__, str(exc)[:120]))


print("models:", httpx.get(base + "/models", headers=hdrs).json())
run("baseline", {})
run("thinking=disabled", {"thinking": {"type": "disabled"}})
run("chat_template_kwargs", {"chat_template_kwargs": {"thinking": False}})
run("reasoning_effort=none", {"reasoning_effort": "none"})
run("nonstream baseline", {}, stream=False)
run("nonstream thinking=disabled", {"thinking": {"type": "disabled"}}, stream=False)
