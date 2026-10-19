#!/usr/bin/env python3
"""
Servidor local do painel Amigas Fashion.

Roda no computador da loja e deixa o painel acessível pelo navegador desse
computador e de celulares/computadores na mesma rede Wi-Fi, com usuário e senha.
Usa só a biblioteca padrão do Python 3.8+ (nada para instalar).

  python servidor.py                      inicia (porta 8080)
  python servidor.py --porta 9000         outra porta
  python servidor.py --criar-usuario      cria ou troca a senha de um usuário
  python servidor.py --importar ARQ.json  importa um backup baixado do painel
  python servidor.py --copia-para PASTA   também guarda as cópias diárias em outra pasta
                                          (pendrive, Google Drive, OneDrive...)

Proteção dos dados:
  - SQLite em modo WAL com sincronização total: cada gravação vai para o disco antes de responder.
  - Tabela "eventos": histórico de todas as alterações (antes e depois), nunca apagado.
  - Cópia diária do banco (.db) e de um arquivo .json legível, guardadas por 60 dias.
  - Verificação de integridade do banco ao iniciar.
  - Visões prontas para Machine Learning: vw_lancamentos, vw_parcelas, vw_contas_pagar.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
from datetime import date
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

PASTA = os.path.dirname(os.path.abspath(__file__))
BANCO = os.path.join(PASTA, "amigas.db")
PASTA_BACKUP = os.path.join(PASTA, "backups")
COLECOES = {"lancamentos", "fiados", "clientes", "contas", "pagar", "lixeira", "config"}
ARQUIVOS = {"/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/chart.umd.js": ("chart.umd.js", "application/javascript")}
SESSAO_DIAS = 30
LOCK = threading.Lock()
TENTATIVAS = {}  # ip -> [horários de tentativas erradas]


# ---------------------------------------------------------------- banco ----
def conectar():
    con = sqlite3.connect(BANCO, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS eventos (seq INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT, usuario TEXT,
                                            acao TEXT, col TEXT, id TEXT, antes TEXT, depois TEXT);
        CREATE TABLE IF NOT EXISTS docs (col TEXT, id TEXT, data TEXT, PRIMARY KEY (col, id));
        CREATE TABLE IF NOT EXISTS usuarios (usuario TEXT PRIMARY KEY, sal TEXT, hash TEXT);
        CREATE TABLE IF NOT EXISTS sessoes (token TEXT PRIMARY KEY, usuario TEXT, expira REAL);
        CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);
    """)
    # Visões para análise e Machine Learning (uma linha por lançamento, parcela e conta)
    try:
        con.executescript("""
        CREATE VIEW IF NOT EXISTS vw_lancamentos AS
          SELECT id, json_extract(data,'$.data') AS data, json_extract(data,'$.tipo') AS tipo,
                 json_extract(data,'$.loja') AS loja, json_extract(data,'$.forma') AS forma,
                 json_extract(data,'$.categoria') AS categoria, json_extract(data,'$.cliente') AS cliente,
                 json_extract(data,'$.conta') AS conta, CAST(json_extract(data,'$.valor') AS REAL) AS valor
          FROM docs WHERE col='lancamentos';
        CREATE VIEW IF NOT EXISTS vw_parcelas AS
          SELECT d.id AS fiado_id, json_extract(d.data,'$.cliente') AS cliente, json_extract(d.data,'$.loja') AS loja,
                 json_extract(d.data,'$.data') AS data_compra, json_extract(p.value,'$.n') AS n_parcela,
                 json_array_length(json_extract(d.data,'$.parcelas')) AS total_parcelas,
                 json_extract(p.value,'$.vencimento') AS vencimento, CAST(json_extract(p.value,'$.valor') AS REAL) AS valor,
                 CAST(json_extract(p.value,'$.pago') AS REAL) AS pago, json_extract(p.value,'$.quitadaEm') AS quitada_em
          FROM docs d, json_each(json_extract(d.data,'$.parcelas')) p WHERE d.col='fiados';
        CREATE VIEW IF NOT EXISTS vw_contas_pagar AS
          SELECT id, json_extract(data,'$.tipo') AS tipo, json_extract(data,'$.loja') AS loja,
                 json_extract(data,'$.vencimento') AS vencimento, CAST(json_extract(data,'$.valor') AS REAL) AS valor,
                 json_extract(data,'$.status') AS status, json_extract(data,'$.pagoEm') AS pago_em
          FROM docs WHERE col='pagar';
        """)
    except sqlite3.Error as e:
        print("Aviso: visões de análise não criadas:", e)
    return con


def registrar(usuario, acao, col, doc_id, antes, depois):
    """Histórico permanente: guarda o conteúdo antes e depois de cada alteração."""
    DB.execute("INSERT INTO eventos (quando, usuario, acao, col, id, antes, depois) VALUES (datetime('now','localtime'),?,?,?,?,?,?)",
               (usuario, acao, col, doc_id, antes, depois))


DB = None


def revisao():
    row = DB.execute("SELECT valor FROM meta WHERE chave='rev'").fetchone()
    return int(row[0]) if row else 0


def subir_revisao():
    DB.execute("INSERT INTO meta VALUES ('rev', ?) ON CONFLICT(chave) DO UPDATE SET valor=?",
               (str(revisao() + 1), str(revisao() + 1)))


def hash_senha(senha, sal):
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), 200_000).hex()


def criar_usuario():
    usuario = input("Nome de usuário: ").strip()
    if not usuario:
        sys.exit("Usuário vazio.")
    while True:
        s1 = getpass.getpass("Senha (mínimo 6 caracteres): ")
        s2 = getpass.getpass("Repita a senha: ")
        if len(s1) >= 6 and s1 == s2:
            break
        print("As senhas não conferem ou são curtas demais. Tente de novo.")
    sal = secrets.token_hex(16)
    with LOCK:
        DB.execute("INSERT OR REPLACE INTO usuarios VALUES (?,?,?)", (usuario, sal, hash_senha(s1, sal)))
        DB.execute("DELETE FROM sessoes WHERE usuario=?", (usuario,))
        DB.commit()
    print(f"Usuário '{usuario}' salvo.")


def importar(caminho):
    with open(caminho, encoding="utf-8") as f:
        pack = json.load(f)
    if pack.get("app") != "amigas-fashion":
        sys.exit("Este arquivo não é um backup do painel Amigas Fashion.")
    n = 0
    with LOCK:
        for col in ("lancamentos", "fiados", "clientes", "contas", "pagar"):
            for d in pack.get(col, []):
                d = dict(d)
                doc_id = d.pop("id", None) or secrets.token_hex(8)
                DB.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?)", (col, doc_id, json.dumps(d, ensure_ascii=False)))
                n += 1
        if pack.get("config"):
            DB.execute("INSERT OR REPLACE INTO docs VALUES ('config','loja',?)", (json.dumps(pack["config"]),))
        subir_revisao()
        DB.commit()
    print(f"{n} registros importados de {os.path.basename(caminho)}.")


COPIA_EXTRA = None


def exportar_json():
    pack = {"app": "amigas-fashion", "versao": 2, "exportadoEm": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for col in ("lancamentos", "fiados", "clientes", "contas", "pagar", "lixeira"):
        pack[col] = []
    with LOCK:
        for col, doc_id, data in DB.execute("SELECT col, id, data FROM docs"):
            if col == "config" and doc_id == "loja":
                pack["config"] = json.loads(data)
            elif col in pack:
                pack[col].append({"id": doc_id, **json.loads(data)})
    return json.dumps(pack, ensure_ascii=False, indent=1)


def backup_diario():
    """Uma cópia por dia do banco (.db) e dos dados (.json) na pasta backups, por 60 dias.
    Com --copia-para, as mesmas cópias também vão para outra pasta."""
    while True:
        try:
            pastas = [PASTA_BACKUP] + ([COPIA_EXTRA] if COPIA_EXTRA else [])
            for pasta in pastas:
                os.makedirs(pasta, exist_ok=True)
                base = os.path.join(pasta, f"amigas-{date.today().isoformat()}")
                if not os.path.exists(base + ".db"):
                    with LOCK:
                        dst = sqlite3.connect(base + ".db")
                        DB.backup(dst)
                        dst.close()
                    with open(base + ".json", "w", encoding="utf-8") as f:
                        f.write(exportar_json())
                for ext in (".db", ".json"):
                    antigos = sorted(f for f in os.listdir(pasta) if f.startswith("amigas-") and f.endswith(ext))
                    for f in antigos[:-60]:
                        os.remove(os.path.join(pasta, f))
        except Exception as e:  # nunca derruba o servidor por causa do backup
            print("Aviso: backup diário falhou:", e)
        time.sleep(3600)


# ---------------------------------------------------------------- http ----
class Handler(BaseHTTPRequestHandler):
    server_version = "AmigasFashion/1.0"

    def log_message(self, fmt, *args):  # silencioso, exceto erros
        pass

    # utilidades
    def responder(self, status, corpo=b"", tipo="application/json", extra=None):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def corpo_json(self):
        tam = int(self.headers.get("Content-Length") or 0)
        if tam > 1_000_000:
            raise ValueError("grande demais")
        return json.loads(self.rfile.read(tam) or b"{}")

    def usuario_logado(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = c["af_sessao"].value if "af_sessao" in c else ""
        if not token:
            return None
        with LOCK:
            row = DB.execute("SELECT usuario, expira FROM sessoes WHERE token=?", (token,)).fetchone()
        if not row or row[1] < time.time():
            return None
        return row[0]

    # rotas
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ARQUIVOS:
            nome, tipo = ARQUIVOS[url.path]
            caminho = os.path.join(PASTA, nome)
            if not os.path.exists(caminho):
                return self.responder(404, b"Arquivo nao encontrado", "text/plain")
            with open(caminho, "rb") as f:
                return self.responder(200, f.read(), tipo)
        if url.path == "/api/me":
            u = self.usuario_logado()
            return self.responder(200, {"usuario": u}) if u else self.responder(401, {"erro": "login"})
        if url.path == "/api/data":
            if not self.usuario_logado():
                return self.responder(401, {"erro": "login"})
            rev_cliente = int((parse_qs(url.query).get("rev") or ["-1"])[0])
            with LOCK:
                rev = revisao()
                if rev == rev_cliente:
                    return self.responder(200, {"rev": rev, "unchanged": True})
                saida = {"rev": rev, "lancamentos": [], "fiados": [], "clientes": [], "contas": [], "pagar": [], "lixeira": [], "config": []}
                for col, doc_id, data in DB.execute("SELECT col, id, data FROM docs"):
                    saida[col].append({"id": doc_id, **json.loads(data)})
            return self.responder(200, saida)
        self.responder(404, {"erro": "não encontrado"})

    def do_POST(self):
        if self.path == "/api/login":
            ip = self.client_address[0]
            agora = time.time()
            recentes = [t for t in TENTATIVAS.get(ip, []) if agora - t < 60]
            if len(recentes) >= 8:
                return self.responder(429, {"erro": "muitas tentativas"})
            try:
                d = self.corpo_json()
            except ValueError:
                return self.responder(400, {"erro": "pedido inválido"})
            with LOCK:
                row = DB.execute("SELECT sal, hash FROM usuarios WHERE usuario=?", (str(d.get("usuario", "")),)).fetchone()
            if not row or not hmac.compare_digest(hash_senha(str(d.get("senha", "")), row[0]), row[1]):
                TENTATIVAS[ip] = recentes + [agora]
                return self.responder(401, {"erro": "usuário ou senha incorretos"})
            token = secrets.token_urlsafe(32)
            with LOCK:
                DB.execute("INSERT INTO sessoes VALUES (?,?,?)", (token, d["usuario"], agora + SESSAO_DIAS * 86400))
                DB.execute("DELETE FROM sessoes WHERE expira < ?", (agora,))
                DB.commit()
            cookie = f"af_sessao={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSAO_DIAS * 86400}"
            return self.responder(200, {"ok": True}, extra={"Set-Cookie": cookie})
        if self.path == "/api/logout":
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if "af_sessao" in c:
                with LOCK:
                    DB.execute("DELETE FROM sessoes WHERE token=?", (c["af_sessao"].value,))
                    DB.commit()
            return self.responder(200, {"ok": True}, extra={"Set-Cookie": "af_sessao=; Path=/; Max-Age=0"})
        self.responder(404, {"erro": "não encontrado"})

    def _doc(self):
        partes = urlparse(self.path).path.split("/")  # ['', 'api', 'doc', col, id]
        if len(partes) != 5 or partes[1:3] != ["api", "doc"] or partes[3] not in COLECOES:
            return None
        doc_id = unquote(partes[4])
        if not doc_id or len(doc_id) > 200:
            return None
        return partes[3], doc_id

    def do_PUT(self):
        usuario = self.usuario_logado()
        if not usuario:
            return self.responder(401, {"erro": "login"})
        alvo = self._doc()
        if not alvo:
            return self.responder(400, {"erro": "caminho inválido"})
        try:
            d = self.corpo_json()
            if not isinstance(d, dict):
                raise ValueError
        except ValueError:
            return self.responder(400, {"erro": "dados inválidos"})
        d.pop("id", None)
        novo = json.dumps(d, ensure_ascii=False)
        with LOCK:
            row = DB.execute("SELECT data FROM docs WHERE col=? AND id=?", alvo).fetchone()
            DB.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?)", (alvo[0], alvo[1], novo))
            registrar(usuario, "alterar" if row else "criar", alvo[0], alvo[1], row[0] if row else None, novo)
            subir_revisao()
            DB.commit()
        self.responder(200, {"ok": True})

    def do_DELETE(self):
        usuario = self.usuario_logado()
        if not usuario:
            return self.responder(401, {"erro": "login"})
        alvo = self._doc()
        if not alvo:
            return self.responder(400, {"erro": "caminho inválido"})
        with LOCK:
            row = DB.execute("SELECT data FROM docs WHERE col=? AND id=?", alvo).fetchone()
            DB.execute("DELETE FROM docs WHERE col=? AND id=?", alvo)
            if row:
                registrar(usuario, "excluir", alvo[0], alvo[1], row[0], None)
            subir_revisao()
            DB.commit()
        self.responder(200, {"ok": True})


def ip_da_rede():
    """Descobre o IP deste computador na rede local (não envia nada pela internet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "IP-DESTE-COMPUTADOR"


def main():
    global DB
    ap = argparse.ArgumentParser(description="Servidor local do painel Amigas Fashion")
    ap.add_argument("--porta", type=int, default=8080)
    ap.add_argument("--criar-usuario", action="store_true")
    ap.add_argument("--importar", metavar="BACKUP.json")
    ap.add_argument("--copia-para", metavar="PASTA", help="pasta extra para as cópias diárias (pendrive, Google Drive...)")
    args = ap.parse_args()

    global COPIA_EXTRA
    COPIA_EXTRA = args.copia_para
    DB = conectar()
    ok = DB.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        print("ATENÇÃO: o banco amigas.db apresentou problema de integridade:", ok)
        print("Restaure a cópia mais recente da pasta backups (copie amigas-AAAA-MM-DD.db para amigas.db).")
    if args.criar_usuario:
        return criar_usuario()
    if args.importar:
        return importar(args.importar)

    if not DB.execute("SELECT 1 FROM usuarios").fetchone():
        print("Primeiro uso: crie o usuário que vai entrar no painel.")
        criar_usuario()
    iniciais = os.path.join(PASTA, "dados-iniciais.json")
    if not DB.execute("SELECT 1 FROM docs").fetchone() and os.path.exists(iniciais):
        importar(iniciais)

    threading.Thread(target=backup_diario, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.porta), Handler)
    ip = ip_da_rede()
    print("\n  Painel Amigas Fashion no ar!")
    print(f"  Neste computador:            http://localhost:{args.porta}")
    print(f"  Celular / outro computador:  http://{ip}:{args.porta}  (mesma rede Wi-Fi)")
    print("  Cópias diárias em:", PASTA_BACKUP + (f"  e  {COPIA_EXTRA}" if COPIA_EXTRA else ""))
    print("  Para desligar, feche esta janela ou aperte Ctrl+C.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Servidor desligado.")


if __name__ == "__main__":
    main()
