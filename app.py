"""
Backend do app LR Climatização.

Usa PostgreSQL (Supabase) quando a variável de ambiente DATABASE_URL existe
— é o caso quando roda no Render.
Se DATABASE_URL não existir, usa SQLite local (arquivo dados.db)
— é o caso quando você testa no seu computador (localhost).

Responde igual à API da Base44, então o HTML quase não muda.

Novidades:
- Lixeira: excluir não apaga na hora, só marca como excluído.
  Fica lá 7 dias, dá pra restaurar. Depois de 7 dias, some pra sempre.
- Configurações: guarda usuário/senha do login no banco (não mais fixo no código).
- Banco persistente: os dados não somem mais quando o Render reinicia ou publica.
"""
import os, json, uuid, datetime, logging, sys, time
from flask import Flask, request, jsonify, g, send_from_directory

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
USANDO_POSTGRES = bool(DATABASE_URL)

if USANDO_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

SENSITIVE_KEYS = {'password','senha','token','authorization','cookie','api_key','apikey','database_url','phone','telefone','email','address','endereco','dados'}
def sanitize(value, key=''):
    if key.lower() in SENSITIVE_KEYS: return '[REDACTED]'
    if isinstance(value, dict): return {str(k): sanitize(v, str(k)) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [sanitize(v) for v in value]
    return value

class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {'level': record.levelname.lower(), 'action': getattr(record, 'action', record.getMessage()), 'at': datetime.datetime.utcnow().isoformat()+'Z'}
        if hasattr(g, 'request_id'): data['requestId'] = g.request_id
        if hasattr(g, 'user_id') and g.user_id: data['userId'] = g.user_id
        fields = getattr(record, 'fields', {})
        data.update(sanitize(fields))
        if record.exc_info: data['exception'] = self.formatException(record.exc_info)
        return json.dumps(sanitize(data), ensure_ascii=False, default=str)
logger = logging.getLogger('lr'); logger.setLevel(os.environ.get('LOG_LEVEL','INFO').upper()); logger.propagate = False
handler = logging.StreamHandler(sys.stdout); handler.setFormatter(JsonFormatter())
if not logger.handlers: logger.addHandler(handler)
def log_event(level, action, **fields):
    logger.log(getattr(logging, level.upper(), logging.INFO), action, extra={'action': action, 'fields': fields})

@app.before_request
def request_context():
    g.request_id = request.headers.get('X-Request-Id') or str(uuid.uuid4())
    g.started_at = time.perf_counter()
    g.user_id = request.headers.get('X-User-Id')
    log_event('info', 'request.start', method=request.method, path=request.path)

@app.after_request
def request_headers(response):
    response.headers['X-Request-Id'] = g.request_id
    log_event('info', 'request.end', method=request.method, path=request.path, status=response.status_code, durationMs=round((time.perf_counter()-getattr(g,'started_at',time.perf_counter()))*1000, 2))
    return response

@app.errorhandler(404)
def not_found(exc):
    return jsonify({'erro':'recurso não encontrado','requestId':g.request_id}), 404

@app.errorhandler(Exception)
def unhandled_error(exc):
    logger.exception('request.failed', extra={'action':'request.failed','fields':{'errorType':type(exc).__name__}})
    return jsonify({'erro':'erro interno','requestId':g.request_id}), 500

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, api_key"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

DB = "dados.db"
DIAS_NA_LIXEIRA = 7

@app.route('/')
def home():
    return send_from_directory('.', 'lr-app-python.html')

@app.route('/icone-lr.svg')
def logo():
    return send_from_directory('.', 'icone-lr.svg')

# ---------------------------------------------------------------------------
# Camada de banco de dados: cada função sabe falar tanto com Postgres quanto
# com SQLite. O "q" troca o placeholder (%s no Postgres, ? no SQLite).
# ---------------------------------------------------------------------------

def get_db():
    if USANDO_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        return conn

def q(sql):
    """Troca os placeholders ? por %s quando estamos no Postgres."""
    return sql.replace("?", "%s") if USANDO_POSTGRES else sql

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS registros (
            id TEXT PRIMARY KEY,
            entidade TEXT NOT NULL,
            dados TEXT NOT NULL,
            criado_em TEXT NOT NULL,
            excluido_em TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)
    cur.execute(q("SELECT 1 FROM config WHERE chave='usuario'"))
    existe = cur.fetchone()
    if not existe:
        cur.execute(q("INSERT INTO config (chave, valor) VALUES ('usuario', 'Luis Rodrigues')"))
        cur.execute(q("INSERT INTO config (chave, valor) VALUES ('senha', 'Luis1994.')"))
    conn.commit()
    cur.close()
    conn.close()

def row_get(row, key):
    """Lê um campo de uma linha, funcionando tanto para dict (Postgres) quanto sqlite3.Row."""
    return row[key]

def limpar_lixeira_antiga():
    limite = (datetime.datetime.utcnow() - datetime.timedelta(days=DIAS_NA_LIXEIRA)).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM registros WHERE excluido_em IS NOT NULL AND excluido_em < ?"), (limite,))
    conn.commit()
    cur.close()
    conn.close()

def row_to_obj(row):
    obj = json.loads(row_get(row, "dados"))
    obj["id"] = row_get(row, "id")
    obj["created_date"] = row_get(row, "criado_em")
    if row_get(row, "excluido_em"):
        obj["excluido_em"] = row_get(row, "excluido_em")
    return obj

@app.route("/api/entities/<entidade>", methods=["GET"])
def listar(entidade):
    conn = get_db()
    cur = conn.cursor()
    sort_by = request.args.get("sort_by", "-criado_em")
    limit = int(request.args.get("limit", 200))
    ordem = "DESC" if sort_by.startswith("-") else "ASC"

    cur.execute(
        q(f"SELECT * FROM registros WHERE entidade=? AND excluido_em IS NULL ORDER BY criado_em {ordem} LIMIT ?"),
        (entidade, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([row_to_obj(r) for r in rows])

@app.route("/api/entities/<entidade>", methods=["POST"])
def criar(entidade):
    dados = request.get_json(force=True) or {}
    novo_id = str(uuid.uuid4())
    agora = datetime.datetime.utcnow().isoformat()

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        q("INSERT INTO registros (id, entidade, dados, criado_em, excluido_em) VALUES (?, ?, ?, ?, NULL)"),
        (novo_id, entidade, json.dumps(dados), agora)
    )
    conn.commit()
    cur.close()
    conn.close()

    dados["id"] = novo_id
    dados["created_date"] = agora
    return jsonify(dados), 201

@app.route("/api/entities/<entidade>/<id>", methods=["PUT"])
def atualizar(entidade, id):
    dados_novos = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT * FROM registros WHERE id=? AND entidade=?"), (id, entidade))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"erro": "não encontrado"}), 404

    atual = json.loads(row_get(row, "dados"))
    atual.update(dados_novos)
    cur.execute(q("UPDATE registros SET dados=? WHERE id=?"), (json.dumps(atual), id))
    conn.commit()
    cur.close()
    conn.close()

    atual["id"] = id
    atual["created_date"] = row_get(row, "criado_em")
    return jsonify(atual)

@app.route("/api/entities/<entidade>/<id>", methods=["DELETE"])
def apagar(entidade, id):
    agora = datetime.datetime.utcnow().isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("UPDATE registros SET excluido_em=? WHERE id=? AND entidade=?"), (agora, id, entidade))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/lixeira", methods=["GET"])
def listar_lixeira():
    limpar_lixeira_antiga()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT * FROM registros WHERE excluido_em IS NOT NULL ORDER BY excluido_em DESC"))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    resultado = []
    for r in rows:
        obj = row_to_obj(r)
        obj["entidade"] = row_get(r, "entidade")
        resultado.append(obj)
    return jsonify(resultado)

@app.route("/api/lixeira/<id>/restaurar", methods=["POST"])
def restaurar(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("UPDATE registros SET excluido_em=NULL WHERE id=?"), (id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/lixeira/<id>", methods=["DELETE"])
def apagar_definitivo(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("DELETE FROM registros WHERE id=?"), (id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/config", methods=["GET"])
def ver_config():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q("SELECT chave, valor FROM config"))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # Nunca devolver a senha para o navegador; somente o endpoint de autenticação compara.
    return jsonify({row_get(r, "chave"): row_get(r, "valor") for r in rows if row_get(r, "chave") != 'senha'})

@app.route('/api/auth', methods=['POST'])
def autenticar():
    dados = request.get_json(silent=True) or {}
    conn = get_db()
    cur = conn.cursor()
    cur.execute(q('SELECT chave, valor FROM config WHERE chave IN (?,?)'), ('usuario', 'senha'))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    cfg = {row_get(r, 'chave'): row_get(r, 'valor') for r in rows}
    ok = dados.get('usuario') == cfg.get('usuario') and dados.get('senha') == cfg.get('senha')
    log_event('info', 'auth.success' if ok else 'auth.failure', method='password')
    return jsonify({'ok': ok}), (200 if ok else 401)

@app.route("/api/config", methods=["PUT"])
def salvar_config():
    dados = request.get_json(force=True) or {}
    conn = get_db()
    cur = conn.cursor()
    # Configurações não sensíveis, como metas mensais, podem ser salvas sem
    # alterar a estrutura do banco. Senha continua sendo tratada como antes.
    for chave, valor in dados.items():
        if chave in ("usuario", "senha") or str(chave).startswith("lr_meta_"):
            if valor is not None and valor != "":
                cur.execute(q("INSERT INTO config (chave, valor) VALUES (?, ?) ON CONFLICT (chave) DO UPDATE SET valor=excluded.valor"), (str(chave), str(valor))) if USANDO_POSTGRES else cur.execute(q("INSERT INTO config (chave, valor) VALUES (?, ?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor"), (str(chave), str(valor)))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"ok": True})

# Cria as tabelas assim que o módulo é importado (necessário no Render, que
# roda o app via gunicorn e nunca passa pelo "if __name__ == '__main__'").
init_db()

if __name__ == "__main__":
    print("Servidor rodando em http://localhost:5000")
    print(f"Banco em uso: {'PostgreSQL (Supabase)' if USANDO_POSTGRES else 'SQLite local (dados.db)'}")
    app.run(host="0.0.0.0", port=5000, debug=False)
