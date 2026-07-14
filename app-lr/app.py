"""
Backend do app LR Climatização.
Guarda tudo num banco SQLite local (arquivo dados.db).
Responde igual à API da Base44, então o HTML quase não muda.

Novidades:
- Lixeira: excluir não apaga na hora, só marca como excluído.
  Fica lá 7 dias, dá pra restaurar. Depois de 7 dias, some pra sempre.
- Configurações: guarda usuário/senha do login no banco (não mais fixo no código).
"""
import sqlite3, json, uuid, datetime, logging, re, sys
from flask import Flask, request, jsonify, g, send_from_directory

app = Flask(__name__)

class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {'level': record.levelname.lower(), 'action': record.getMessage(), 'at': datetime.datetime.utcnow().isoformat()+'Z'}
        if hasattr(g, 'request_id'): data['requestId'] = g.request_id
        if request and request.headers.get('X-User-Id'): data['userId'] = request.headers.get('X-User-Id')
        return json.dumps(data, ensure_ascii=False)
logger = logging.getLogger('lr'); logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout); handler.setFormatter(JsonFormatter()); logger.addHandler(handler)

@app.before_request
def request_context():
    g.request_id = request.headers.get('X-Request-Id') or str(uuid.uuid4())
    logger.info('request.start %s %s', request.method, request.path)

@app.after_request
def request_headers(response):
    response.headers['X-Request-Id'] = g.request_id
    return response

@app.errorhandler(404)
def not_found(exc):
    return jsonify({'erro':'recurso não encontrado','requestId':g.request_id}), 404

@app.errorhandler(Exception)
def unhandled_error(exc):
    logger.exception('request.failed')
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

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registros (
            id TEXT PRIMARY KEY,
            entidade TEXT NOT NULL,
            dados TEXT NOT NULL,
            criado_em TEXT NOT NULL,
            excluido_em TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)
    existe = conn.execute("SELECT 1 FROM config WHERE chave='usuario'").fetchone()
    if not existe:
        conn.execute("INSERT INTO config (chave, valor) VALUES ('usuario', 'Luis Rodrigues')")
        conn.execute("INSERT INTO config (chave, valor) VALUES ('senha', 'Luis1994.')")
    conn.commit()
    conn.close()

def limpar_lixeira_antiga():
    limite = (datetime.datetime.utcnow() - datetime.timedelta(days=DIAS_NA_LIXEIRA)).isoformat()
    conn = get_db()
    conn.execute("DELETE FROM registros WHERE excluido_em IS NOT NULL AND excluido_em < ?", (limite,))
    conn.commit()
    conn.close()

def row_to_obj(row):
    obj = json.loads(row["dados"])
    obj["id"] = row["id"]
    obj["created_date"] = row["criado_em"]
    if row["excluido_em"]:
        obj["excluido_em"] = row["excluido_em"]
    return obj

@app.route("/api/entities/<entidade>", methods=["GET"])
def listar(entidade):
    conn = get_db()
    sort_by = request.args.get("sort_by", "-criado_em")
    limit = int(request.args.get("limit", 200))
    ordem = "DESC" if sort_by.startswith("-") else "ASC"

    rows = conn.execute(
        f"SELECT * FROM registros WHERE entidade=? AND excluido_em IS NULL ORDER BY criado_em {ordem} LIMIT ?",
        (entidade, limit)
    ).fetchall()
    conn.close()
    return jsonify([row_to_obj(r) for r in rows])

@app.route("/api/entities/<entidade>", methods=["POST"])
def criar(entidade):
    dados = request.get_json(force=True) or {}
    novo_id = str(uuid.uuid4())
    agora = datetime.datetime.utcnow().isoformat()

    conn = get_db()
    conn.execute(
        "INSERT INTO registros (id, entidade, dados, criado_em, excluido_em) VALUES (?, ?, ?, ?, NULL)",
        (novo_id, entidade, json.dumps(dados), agora)
    )
    conn.commit()
    conn.close()

    dados["id"] = novo_id
    dados["created_date"] = agora
    return jsonify(dados), 201

@app.route("/api/entities/<entidade>/<id>", methods=["PUT"])
def atualizar(entidade, id):
    dados_novos = request.get_json(force=True) or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM registros WHERE id=? AND entidade=?", (id, entidade)).fetchone()
    if not row:
        conn.close()
        return jsonify({"erro": "não encontrado"}), 404

    atual = json.loads(row["dados"])
    atual.update(dados_novos)
    conn.execute("UPDATE registros SET dados=? WHERE id=?", (json.dumps(atual), id))
    conn.commit()
    conn.close()

    atual["id"] = id
    atual["created_date"] = row["criado_em"]
    return jsonify(atual)

@app.route("/api/entities/<entidade>/<id>", methods=["DELETE"])
def apagar(entidade, id):
    agora = datetime.datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute("UPDATE registros SET excluido_em=? WHERE id=? AND entidade=?", (agora, id, entidade))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/lixeira", methods=["GET"])
def listar_lixeira():
    limpar_lixeira_antiga()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM registros WHERE excluido_em IS NOT NULL ORDER BY excluido_em DESC"
    ).fetchall()
    conn.close()
    resultado = []
    for r in rows:
        obj = row_to_obj(r)
        obj["entidade"] = r["entidade"]
        resultado.append(obj)
    return jsonify(resultado)

@app.route("/api/lixeira/<id>/restaurar", methods=["POST"])
def restaurar(id):
    conn = get_db()
    conn.execute("UPDATE registros SET excluido_em=NULL WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/lixeira/<id>", methods=["DELETE"])
def apagar_definitivo(id):
    conn = get_db()
    conn.execute("DELETE FROM registros WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/config", methods=["GET"])
def ver_config():
    conn = get_db()
    rows = conn.execute("SELECT chave, valor FROM config").fetchall()
    conn.close()
    # Nunca devolver a senha para o navegador; somente o endpoint de autenticação compara.
    return jsonify({r["chave"]: r["valor"] for r in rows if r["chave"] != 'senha'})

@app.route('/api/auth', methods=['POST'])
def autenticar():
    dados = request.get_json(silent=True) or {}
    conn = get_db(); rows = conn.execute('SELECT chave, valor FROM config WHERE chave IN (?,?)', ('usuario','senha')).fetchall(); conn.close()
    cfg = {r['chave']: r['valor'] for r in rows}
    ok = dados.get('usuario') == cfg.get('usuario') and dados.get('senha') == cfg.get('senha')
    logger.info('auth.%s', 'success' if ok else 'failure')
    return jsonify({'ok': ok}), (200 if ok else 401)

@app.route("/api/config", methods=["PUT"])
def salvar_config():
    dados = request.get_json(force=True) or {}
    conn = get_db()
    for chave in ("usuario", "senha"):
        if chave in dados and dados[chave]:
            conn.execute("UPDATE config SET valor=? WHERE chave=?", (dados[chave], chave))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

if __name__ == "__main__":
    init_db()
    print("Servidor rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
