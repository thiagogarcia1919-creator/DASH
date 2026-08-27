import calendar
import copy
import hmac
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, make_response, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / ".data")))
DATA_FILE = DATA_DIR / "dashboard_state.json"
PAINEL_KEY = os.getenv("PAINEL_KEY", "").strip()

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("painel-lark")
lock = threading.RLock()

DEFAULT_STATE = {
    "linha": "LINHA 04",
    "unidade": "CONDENSADORA",
    "mes": "",
    "ano": 0,
    "planoMensal": 0,
    "metaOEE": 0.92,
    "metaAbs": 0.024,
    "metaDowntimeMin": 30,
    "turnos": [],
    "lotes": [],
}


def _save_state(state):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_FILE)
    except Exception as exc:
        log.warning("Nao foi possivel salvar o snapshot local: %s", exc)


def _load_state():
    try:
        if DATA_FILE.exists():
            loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return {**copy.deepcopy(DEFAULT_STATE), **loaded}
    except Exception as exc:
        log.warning("Snapshot local ignorado: %s", exc)
    return copy.deepcopy(DEFAULT_STATE)


STATE = _load_state()


def norm_key(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def pick(record, *names, default=None):
    if not isinstance(record, dict):
        return default
    normalized = {norm_key(k): v for k, v in record.items()}
    for name in names:
        key = norm_key(name)
        if key in normalized:
            return normalized[key]
    return default


def num(value, default=0.0):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "")
    s = s.replace("R$", "").replace("%", "")
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s or s in {"-", ".", ","}:
        return default
    try:
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
        return float(s)
    except ValueError:
        return default


def integer(value, default=0):
    return int(round(num(value, default)))


def percent(value, default=0.0):
    if value is None or value == "":
        return default
    had_percent = isinstance(value, str) and "%" in value
    n = num(value, default)
    if had_percent or n > 1.5:
        n /= 100.0
    return n


def iso_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        n = float(value)
        try:
            if n > 10_000_000_000:
                n /= 1000.0
            if n > 1_000_000_000:
                return datetime.fromtimestamp(n).date().isoformat()
        except Exception:
            pass
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def turno_num(value):
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n in (1, 2, 3) else 1
    m = re.search(r"([123])", str(value or ""))
    return int(m.group(1)) if m else 1


def duration_min(value):
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(round(float(value))))
    s = str(value).strip().lower()
    m = re.fullmatch(r"(\d{1,3}):(\d{1,2})(?::\d{1,2})?", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    h = re.search(r"(\d+(?:[.,]\d+)?)\s*h", s)
    mi = re.search(r"(\d+(?:[.,]\d+)?)\s*m", s)
    if h or mi:
        return int(round((num(h.group(1)) if h else 0) * 60 + (num(mi.group(1)) if mi else 0)))
    return max(0, integer(s))


def days_remaining(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
        last = calendar.monthrange(d.year, d.month)[1]
        return last - d.day + 1
    except Exception:
        return 0


def flatten_record(record):
    if not isinstance(record, dict):
        return record
    fields = record.get("fields")
    if isinstance(fields, dict):
        merged = dict(fields)
        for k, v in record.items():
            if k != "fields" and k not in merged:
                merged[k] = v
        return merged
    return record


def _jsonish(value):
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") or s.startswith("{"):
            try:
                return json.loads(s)
            except Exception:
                return value
    return value


def extract_records(payload, preferred_keys):
    payload = _jsonish(payload)
    if isinstance(payload, list):
        return [flatten_record(x) for x in payload if isinstance(x, dict)], True
    if not isinstance(payload, dict):
        return [], False

    for key in preferred_keys:
        value = _jsonish(pick(payload, key))
        if isinstance(value, list):
            return [flatten_record(x) for x in value if isinstance(x, dict)], True
        if isinstance(value, dict):
            # Alguns fluxos do Lark embrulham o resultado em {items:[...]}
            for inner_key in ("items", "records", "registros", "data", "value", "values"):
                inner = _jsonish(pick(value, inner_key))
                if isinstance(inner, list):
                    return [flatten_record(x) for x in inner if isinstance(x, dict)], True

    return [flatten_record(payload)], False


def request_authorized():
    if not PAINEL_KEY:
        return True
    supplied = (
        request.headers.get("X-Painel-Key", "").strip()
        or request.args.get("key", "").strip()
    )
    auth = request.headers.get("Authorization", "").strip()
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, PAINEL_KEY)


def protected(fn):
    def wrapper(*args, **kwargs):
        if not request_authorized():
            return jsonify({"ok": False, "erro": "nao autorizado"}), 401
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def parse_oee(record):
    data = iso_date(pick(record, "data", "fórmula", "formula", "data operacional", "dia"))
    if not data:
        return None
    plano = integer(pick(record, "plano", "plano diário", "plano diario", "meta do turno"))
    real = integer(pick(record, "real", "produção real", "producao real", "produzido"))
    faltas = integer(pick(record, "faltas", "faltas no turno"))
    efetivo = integer(pick(record, "efetivo", "efetivo previsto", "headcount"))
    downtime = duration_min(pick(record, "dtMin", "downtime", "DOWNTIME", "tempo de parada"))
    dias = integer(pick(record, "diasRest", "dias restantes"), days_remaining(data))
    oee = percent(pick(record, "oee", "OEE"))
    abs_ = percent(pick(record, "abs_", "absenteísmo %", "absenteismo %", "absenteísmo", "absenteismo"))
    return {
        "data": data,
        "turno": turno_num(pick(record, "turno", "Turno")),
        "plano": plano,
        "real": real,
        "gap": integer(pick(record, "gap", "Gap"), real - plano),
        "diasRest": dias,
        "necDia": integer(pick(record, "necDia", "necessário por dia", "necessario por dia")),
        "faltas": faltas,
        "efetivo": efetivo,
        "abs_": abs_,
        "oee": oee,
        "dtMin": downtime,
        "acumProd": integer(pick(record, "acumProd", "produzido no mês", "produzido no mes", "acumulado", "produção acumulada", "producao acumulada")),
    }


def enrich_turnos(turnos):
    # Mantem somente linhas válidas e recalcula acumulado em ordem cronológica.
    turnos = [x for x in turnos if isinstance(x, dict) and x.get("data")]
    turnos.sort(key=lambda x: (x.get("data", ""), int(x.get("turno", 0))))
    running = 0
    for row in turnos:
        running += integer(row.get("real"))
        explicit = integer(row.get("acumProd"))
        row["acumProd"] = max(running, explicit)
        if not row.get("diasRest"):
            row["diasRest"] = days_remaining(row["data"])
        if not row.get("gap") and (row.get("plano") or row.get("real")):
            row["gap"] = integer(row.get("real")) - integer(row.get("plano"))
    return turnos


def normalize_status(value):
    raw = unicodedata.normalize("NFKD", str(value or "").strip().upper())
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    if any(x in raw for x in ("PRODUZINDO", "EM PRODUCAO", "EM PROCESSO")):
        return "PRODUZINDO"
    if any(x in raw for x in ("PRODUZIDO", "FINALIZADO", "CONCLUIDO", "ENCERRADO")):
        return "PRODUZIDO"
    return "AG. PRODUZIR"


def parse_lote(record):
    linha = str(pick(record, "linha", "linha de produção", "linha de producao", default="LINHA 04") or "LINHA 04").strip().upper()
    lote = str(pick(record, "lote", "LOTE", default="") or "").strip()
    op = str(pick(record, "op", "ordem", "ordem de produção", "ordem de producao", default="") or "").strip()
    modelo = str(pick(record, "modelo", "descrição do modelo", "descricao do modelo", default="") or "").strip()
    serie = str(pick(record, "serie", "série", "série do produto", "serie do produto", default="") or "").strip()
    qtd = integer(pick(record, "qtd", "quantidade", "qtd lote", "__QTD__", "QTD"))
    prod = integer(pick(record, "prod", "produzido", "qtd. total produzida", "qtd total produzida"))
    falta_raw = pick(record, "falta", "falta produzir", "saldo", "saldo a produzir")
    falta = integer(falta_raw, max(0, qtd - prod))
    status = normalize_status(pick(record, "status", "status de produção", "status de producao"))
    pend = integer(pick(record, "pend", "pendência", "pendencia"), falta if status == "PRODUZIDO" and falta > 0 else 0)
    return {
        "linha": linha,
        "lote": lote,
        "op": op,
        "cod": str(pick(record, "cod", "código do produto", "codigo do produto", default="") or "").strip(),
        "modelo": modelo,
        "serie": serie,
        "unidade": str(pick(record, "unidade", default="") or "").strip(),
        "qtd": qtd,
        "prod": prod,
        "falta": falta,
        "status": status,
        "ini": str(pick(record, "ini", "prev. início", "prev inicio", default="") or "").strip(),
        "fim": str(pick(record, "fim", "prev. término", "prev termino", default="") or "").strip(),
        "caphr": integer(pick(record, "caphr", "cap/hr")),
        "prazo": str(pick(record, "prazo", "cálculo de atraso de produção", "calculo de atraso de producao", default="") or "").strip(),
        "obs": str(pick(record, "obs", "observação", "observacao", default="") or "").strip(),
        "importacao": "",
        "pend": pend,
    }


def is_delete(record):
    action = str(pick(record, "acao", "ação", "action", "evento", default="") or "").strip().lower()
    deleted = pick(record, "deleted", "excluido", "excluído", "removido", default=False)
    return action in {"delete", "deleted", "excluir", "excluido", "remover", "removido"} or deleted is True


def update_top_level_from_payload(payload):
    if not isinstance(payload, dict):
        return
    linha = pick(payload, "linha", "linha de produção", "linha de producao")
    unidade = pick(payload, "unidade")
    plano_mensal = pick(payload, "planoMensal", "plano mensal")
    meta_oee = pick(payload, "metaOEE", "meta oee")
    meta_abs = pick(payload, "metaAbs", "meta absenteísmo", "meta absenteismo")
    meta_down = pick(payload, "metaDowntimeMin", "meta downtime", "meta downtime min")
    if linha:
        STATE["linha"] = str(linha).strip().upper()
    if unidade:
        STATE["unidade"] = str(unidade).strip().upper()
    if plano_mensal not in (None, ""):
        STATE["planoMensal"] = integer(plano_mensal)
    if meta_oee not in (None, ""):
        STATE["metaOEE"] = percent(meta_oee)
    if meta_abs not in (None, ""):
        STATE["metaAbs"] = percent(meta_abs)
    if meta_down not in (None, ""):
        STATE["metaDowntimeMin"] = integer(meta_down)


@app.get("/")
def dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "servico": "painel-lark"})


@app.get("/api/dashboard")
def api_dashboard():
    with lock:
        payload = copy.deepcopy(STATE)
    resp = make_response(jsonify(payload))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/api/status")
def api_status():
    with lock:
        return jsonify({
            "ok": True,
            "turnos": len(STATE.get("turnos", [])),
            "lotes": len(STATE.get("lotes", [])),
            "linha": STATE.get("linha"),
            "planoMensal": STATE.get("planoMensal"),
            "protegido": bool(PAINEL_KEY),
        })


@app.post("/lark/oee")
@protected
def lark_oee():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "erro": "envie JSON no corpo da solicitacao"}), 400

    records, full_package = extract_records(payload, ("turnos", "registros", "records", "items", "dados", "data"))
    parsed = [parse_oee(r) for r in records]
    parsed = [r for r in parsed if r]
    if not parsed:
        return jsonify({"ok": False, "erro": "nenhum registro OEE valido encontrado"}), 400

    with lock:
        update_top_level_from_payload(payload if isinstance(payload, dict) else {})
        # Também aceita Plano Mensal e metadados dentro das linhas pesquisadas.
        for raw in records:
            if isinstance(raw, dict):
                pm = pick(raw, "planoMensal", "plano mensal")
                if pm not in (None, ""):
                    STATE["planoMensal"] = integer(pm)
                linha = pick(raw, "linha", "linha de produção", "linha de producao")
                unidade = pick(raw, "unidade")
                if linha:
                    STATE["linha"] = str(linha).strip().upper()
                if unidade:
                    STATE["unidade"] = str(unidade).strip().upper()

        replace = full_package or bool(pick(payload, "substituir", "replace", default=False)) if isinstance(payload, dict) else full_package
        if replace:
            STATE["turnos"] = enrich_turnos(parsed)
        else:
            current = {(x.get("data"), int(x.get("turno", 0))): x for x in STATE.get("turnos", [])}
            for row in parsed:
                current[(row["data"], row["turno"])] = row
            STATE["turnos"] = enrich_turnos(list(current.values()))
        _save_state(STATE)
        total = len(STATE["turnos"])

    log.info("OEE recebido: %s registro(s); total=%s", len(parsed), total)
    return jsonify({"ok": True, "recebidos": len(parsed), "turnos": total, "modo": "substituir" if replace else "atualizar"})


@app.post("/lark/programacao")
@protected
def lark_programacao():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "erro": "envie JSON no corpo da solicitacao"}), 400

    records, full_package = extract_records(payload, ("lotes", "registros", "records", "items", "dados", "data"))
    if not records:
        return jsonify({"ok": False, "erro": "nenhum registro de programacao encontrado"}), 400

    with lock:
        update_top_level_from_payload(payload if isinstance(payload, dict) else {})
        replace = full_package or bool(pick(payload, "substituir", "replace", default=False)) if isinstance(payload, dict) else full_package
        if replace:
            parsed = [parse_lote(r) for r in records if not is_delete(r)]
            STATE["lotes"] = parsed
            received = len(parsed)
        else:
            current = {}
            for x in STATE.get("lotes", []):
                key = x.get("op") or x.get("lote")
                if key:
                    current[key] = x
            received = 0
            for raw in records:
                row = parse_lote(raw)
                key = row.get("op") or row.get("lote")
                if not key:
                    continue
                if is_delete(raw):
                    current.pop(key, None)
                else:
                    current[key] = row
                    received += 1
            STATE["lotes"] = list(current.values())
        _save_state(STATE)
        total = len(STATE["lotes"])

    log.info("Programacao recebida: %s registro(s); total=%s", received, total)
    return jsonify({"ok": True, "recebidos": received, "lotes": total, "modo": "substituir" if replace else "atualizar"})


@app.post("/lark/reset")
@protected
def lark_reset():
    global STATE
    with lock:
        STATE = copy.deepcopy(DEFAULT_STATE)
        _save_state(STATE)
    return jsonify({"ok": True, "mensagem": "painel zerado"})


@app.errorhandler(404)
def not_found(_):
    return jsonify({"ok": False, "erro": "rota nao encontrada"}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
