import calendar
import copy
import hmac
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
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


def id_text(value):
    """Normaliza identificadores do Lark (SEQ/Item) sem perder o valor."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        n = float(value)
        return str(int(n)) if n.is_integer() else str(value).strip()
    s = str(value).strip()
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".", 1)[0]
    return s


def iso_date(value):
    """Normaliza datas do Lark/Excel para YYYY-MM-DD."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        n = float(value)
        try:
            if 20_000 <= n <= 100_000:
                return (datetime(1899, 12, 30) + timedelta(days=n)).date().isoformat()
            if n > 10_000_000_000:
                n /= 1000.0
            if n > 1_000_000_000:
                return datetime.fromtimestamp(n).date().isoformat()
        except Exception:
            pass

    s = str(value).strip()

    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            pass

    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a, b, y = map(int, m.groups())
        if a > 12 and b <= 12:       # DD/MM/YYYY
            day, month = a, b
        elif b > 12 and a <= 12:     # MM/DD/YYYY
            month, day = a, b
        else:
            # Na Solicitação HTTP do Lark, datas ambíguas vêm normalmente MM/DD/YYYY.
            month, day = a, b
        try:
            return date(y, month, day).isoformat()
        except ValueError:
            return None

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
        "seq": id_text(pick(record, "seq", "SEQ")),
        "linha": str(pick(record, "linha", "linha de produção", "linha de producao", default="LINHA 04") or "LINHA 04").strip().upper(),
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
        "planoMensal": integer(pick(record, "planoMensal", "plano mensal")),
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
        "item": id_text(pick(record, "item", "Item")),
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


def same_oee_record(a, b):
    """OEE é identificado pelo SEQ. Linha pode mudar."""
    seq_a, seq_b = id_text(a.get("seq")), id_text(b.get("seq"))
    if seq_a and seq_b:
        return seq_a == seq_b
    return (
        a.get("data") == b.get("data")
        and int(a.get("turno", 0)) == int(b.get("turno", 0))
        and str(a.get("linha") or "").strip().upper()
            == str(b.get("linha") or "").strip().upper()
    )


def same_lote_record(a, b):
    """Programação é identificada pelo Item. Linha pode mudar."""
    item_a, item_b = id_text(a.get("item")), id_text(b.get("item"))
    if item_a and item_b:
        return item_a == item_b
    op_a, op_b = str(a.get("op") or "").strip(), str(b.get("op") or "").strip()
    if op_a and op_b:
        return op_a == op_b
    lote_a, lote_b = str(a.get("lote") or "").strip(), str(b.get("lote") or "").strip()
    return bool(lote_a and lote_b and lote_a == lote_b)


def recompute_month_plan():
    """Plano mensal sempre acompanha os registros OEE atualmente armazenados."""
    valores = [integer(x.get("planoMensal")) for x in STATE.get("turnos", [])]
    valores = [x for x in valores if x > 0]
    STATE["planoMensal"] = max(valores) if valores else 0


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

    records, full_package = extract_records(
        payload, ("turnos", "registros", "records", "items", "dados", "data")
    )
    replace = (
        full_package
        or bool(pick(payload, "substituir", "replace", default=False))
        if isinstance(payload, dict)
        else full_package
    )

    with lock:
        update_top_level_from_payload(payload if isinstance(payload, dict) else {})

        # SINCRONIZAÇÃO COMPLETA:
        # inclusive {"substituir": true, "registros": []} zera a Base OEE.
        if replace:
            parsed = []
            for raw in records:
                row = parse_oee(raw)
                if row:
                    parsed.append(row)

            STATE["turnos"] = enrich_turnos(parsed)
            recompute_month_plan()
            _save_state(STATE)

            return jsonify({
                "ok": True,
                "recebidos": len(parsed),
                "turnos": len(STATE["turnos"]),
                "modo": "substituir"
            })

        # ATUALIZAÇÃO UNITÁRIA:
        # Se um registro ainda tem SEQ, mas ficou sem Data, ele foi limpo no Lark.
        # Nesse caso removemos o snapshot antigo daquele SEQ.
        current = list(STATE.get("turnos", []))
        recebidos = 0
        removidos = 0

        for raw in records:
            if not isinstance(raw, dict):
                continue

            seq = id_text(pick(raw, "seq", "SEQ"))
            row = parse_oee(raw)

            if row:
                antes = len(current)
                current = [x for x in current if not same_oee_record(x, row)]
                current.append(row)
                recebidos += 1
            elif seq:
                antes = len(current)
                current = [x for x in current if id_text(x.get("seq")) != seq]
                removidos += antes - len(current)

        if recebidos == 0 and removidos == 0:
            return jsonify({
                "ok": False,
                "erro": "nenhum registro OEE valido encontrado"
            }), 400

        STATE["turnos"] = enrich_turnos(current)
        recompute_month_plan()
        _save_state(STATE)

        return jsonify({
            "ok": True,
            "recebidos": recebidos,
            "removidos": removidos,
            "turnos": len(STATE["turnos"]),
            "modo": "atualizar"
        })


@app.post("/lark/oee/delete")
@protected
def lark_oee_delete():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "erro": "envie JSON no corpo da solicitacao"}), 400

    seq = id_text(pick(payload, "seq", "SEQ"))
    data = iso_date(pick(payload, "data", "fórmula", "formula", "data operacional", "dia"))
    turno_raw = pick(payload, "turno", "Turno")

    if not seq and (not data or turno_raw in (None, "")):
        return jsonify({
            "ok": False,
            "erro": "informe seq ou data+turno do registro excluido"
        }), 400

    turno = turno_num(turno_raw) if turno_raw not in (None, "") else None

    with lock:
        antes = len(STATE.get("turnos", []))

        if seq:
            STATE["turnos"] = [
                x for x in STATE.get("turnos", [])
                if id_text(x.get("seq")) != seq
            ]
        else:
            STATE["turnos"] = [
                x for x in STATE.get("turnos", [])
                if not (
                    x.get("data") == data
                    and int(x.get("turno", 0)) == turno
                )
            ]

        STATE["turnos"] = enrich_turnos(STATE["turnos"])
        recompute_month_plan()
        _save_state(STATE)
        depois = len(STATE["turnos"])

    return jsonify({
        "ok": True,
        "removidos": antes - depois,
        "turnos": depois,
        "seq": seq
    })


@app.post("/lark/programacao")
@protected
def lark_programacao():
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "erro": "envie JSON no corpo da solicitacao"}), 400

    records, full_package = extract_records(
        payload, ("lotes", "registros", "records", "items", "dados", "data")
    )
    replace = (
        full_package
        or bool(pick(payload, "substituir", "replace", default=False))
        if isinstance(payload, dict)
        else full_package
    )

    with lock:
        update_top_level_from_payload(payload if isinstance(payload, dict) else {})

        if replace:
            parsed = [
                parse_lote(r) for r in records
                if isinstance(r, dict) and not is_delete(r)
            ]
            parsed = [
                r for r in parsed
                if r.get("item") or r.get("op") or r.get("lote")
            ]
            STATE["lotes"] = parsed
            _save_state(STATE)
            return jsonify({
                "ok": True,
                "recebidos": len(parsed),
                "lotes": len(STATE["lotes"]),
                "modo": "substituir"
            })

        current = list(STATE.get("lotes", []))
        recebidos = 0
        removidos = 0

        for raw in records:
            if not isinstance(raw, dict):
                continue

            item = id_text(pick(raw, "item", "Item"))
            row = parse_lote(raw)

            if is_delete(raw):
                antes = len(current)
                current = [x for x in current if id_text(x.get("item")) != item]
                removidos += antes - len(current)
                continue

            if not (row.get("item") or row.get("op") or row.get("lote")):
                if item:
                    antes = len(current)
                    current = [x for x in current if id_text(x.get("item")) != item]
                    removidos += antes - len(current)
                continue

            current = [x for x in current if not same_lote_record(x, row)]
            current.append(row)
            recebidos += 1

        if recebidos == 0 and removidos == 0:
            return jsonify({
                "ok": False,
                "erro": "nenhum registro de programacao encontrado"
            }), 400

        STATE["lotes"] = current
        _save_state(STATE)

        return jsonify({
            "ok": True,
            "recebidos": recebidos,
            "removidos": removidos,
            "lotes": len(STATE["lotes"]),
            "modo": "atualizar"
        })


@app.post("/lark/programacao/delete")
@protected
def lark_programacao_delete():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "erro": "envie JSON no corpo da solicitacao"}), 400

    item = id_text(pick(payload, "item", "Item"))
    linha = str(pick(payload, "linha", "linha de produção", "linha de producao", default="") or "").strip().upper()
    if not item:
        return jsonify({"ok": False, "erro": "informe item do registro excluido"}), 400

    with lock:
        antes = len(STATE.get("lotes", []))
        STATE["lotes"] = [
            x for x in STATE.get("lotes", [])
            if not (
                id_text(x.get("item")) == item
                and (not linha or str(x.get("linha") or "").strip().upper() == linha)
            )
        ]
        _save_state(STATE)
        depois = len(STATE["lotes"])

    removidos = antes - depois
    log.info("Exclusao programacao recebida: item=%s linha=%s removidos=%s", item, linha, removidos)
    return jsonify({"ok": True, "removidos": removidos, "lotes": depois, "item": item, "linha": linha})


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
