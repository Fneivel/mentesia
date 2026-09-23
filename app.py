"""
MentesIA — site + painel administrativo.

Rodar local:   python app.py          (http://127.0.0.1:5000  |  painel em /admin)
Produção:      gunicorn app:app
Configuração:  variáveis de ambiente (veja .env.example)
"""
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from datetime import datetime
from functools import wraps

import markdown as md
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path=os.path.join(BASE_DIR, ".env")):
    """Lê um arquivo .env simples (CHAVE=valor), sem dependência extra."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "mentesia.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_pw_hash = os.environ.get("ADMIN_PASSWORD_HASH")
_pw_plain = os.environ.get("ADMIN_PASSWORD")
ADMIN_PASSWORD_HASH = _pw_hash or (generate_password_hash(_pw_plain) if _pw_plain else None)

IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif", "svg"}
FILE_EXT = IMAGE_EXT | {"pdf", "zip", "xlsx", "docx", "pptx", "csv", "txt"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PRODUCTION", "0") == "1",
)

# =====================================================================
# Banco de dados
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  titulo TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, categoria TEXT DEFAULT '',
  resumo TEXT DEFAULT '', conteudo TEXT DEFAULT '', tempo INTEGER DEFAULT 5,
  capa TEXT DEFAULT '', destaque INTEGER DEFAULT 0, ativo INTEGER DEFAULT 1,
  ordem INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS materiais (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  titulo TEXT NOT NULL, formato TEXT DEFAULT '', descricao TEXT DEFAULT '',
  itens TEXT DEFAULT '', link TEXT DEFAULT '', arquivo TEXT DEFAULT '',
  botao TEXT DEFAULT 'Quero o material grátis',
  ativo INTEGER DEFAULT 1, ordem INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS produtos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  titulo TEXT NOT NULL, tipo TEXT DEFAULT '', capa_texto TEXT DEFAULT '', imagem TEXT DEFAULT '',
  descricao TEXT DEFAULT '', itens TEXT DEFAULT '', preco REAL DEFAULT 0, preco_antigo REAL,
  parcelas INTEGER DEFAULT 12, selo TEXT DEFAULT '', link TEXT DEFAULT '',
  botao TEXT DEFAULT 'Comprar na Hotmart',
  ativo INTEGER DEFAULT 1, ordem INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  texto TEXT NOT NULL, tag TEXT DEFAULT '',
  ativo INTEGER DEFAULT 1, ordem INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS inscritos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE, origem TEXT DEFAULT 'site', criado_em TEXT
);
CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "artigo"


# =====================================================================
# Textos editáveis do site (tela "Textos do site")
# =====================================================================
SETTINGS = [
    ("Topo da página", [
        ("hero_eyebrow", "Linha acima do título", "text", "Blog · Materiais · Cursos"),
        ("hero_titulo", "Título principal", "text", "Inteligência artificial"),
        ("hero_destaque", "Parte do título em degradê", "text", "sem mistério."),
        ("hero_subtitulo", "Texto de apoio", "textarea",
         "Novidades de IA traduzidas para o português do dia a dia, materiais gratuitos para começar "
         "hoje e cursos práticos para quem quer usar IA no trabalho de verdade."),
        ("hero_botao1", "Botão principal", "text", "Baixar material grátis"),
        ("hero_botao2", "Botão secundário", "text", "Ler as novidades"),
        ("hero_selos", "Selos abaixo dos botões (um por linha)", "lines",
         "Conteúdo em português\nMateriais 100% gratuitos\nCompra segura via Hotmart"),
    ]),
    ("Seção de novidades", [
        ("blog_titulo", "Título", "text", "Novidades e guias"),
        ("blog_texto", "Texto", "textarea", "O que muda com cada ferramenta nova, explicado sem jargão e com exemplo de uso."),
    ]),
    ("Seção de materiais gratuitos", [
        ("gratis_titulo", "Título", "text", "Comece sem gastar nada"),
        ("gratis_texto", "Texto", "textarea", "Guias, checklists e planilhas para dar os primeiros passos. Você recebe o acesso no e-mail."),
    ]),
    ("Loja", [
        ("loja_titulo", "Título", "text", "Cursos e produtos"),
        ("loja_texto", "Texto", "textarea", "Conteúdo aprofundado para aplicar IA no trabalho e nos negócios. O pagamento e o acesso são feitos pela Hotmart."),
        ("loja_seguranca", "Aviso abaixo dos produtos", "text", "Pagamento processado pela Hotmart: Pix, boleto e cartão em até 12x."),
    ]),
    ("Newsletter", [
        ("news_titulo", "Título", "text", "Um e-mail por semana com o que importa em IA."),
        ("news_texto", "Texto", "textarea", "Ferramentas novas, um prompt pronto para usar e aviso antecipado de materiais gratuitos."),
        ("news_sucesso", "Mensagem após inscrição", "text", "Pronto! Seu e-mail foi cadastrado."),
    ]),
    ("Perguntas frequentes", [
        ("faq", "Perguntas e respostas", "faq",
         "Preciso saber programar para aproveitar os materiais?\nNão. Os materiais são feitos para quem usa o computador no trabalho e quer ganhar tempo com IA, sem precisar escrever código.\n\n"
         "Como recebo o acesso depois da compra?\nA Hotmart envia o acesso para o e-mail usado na compra. Você também encontra o produto na área “Minhas compras” da sua conta Hotmart.\n\n"
         "Tem garantia?\nSim. Todos os produtos têm garantia de 7 dias pela Hotmart: se não gostar, você pede o reembolso direto na plataforma.\n\n"
         "Os materiais gratuitos são mesmo gratuitos?\nSão. Você só informa seu e-mail para receber o link de acesso."),
    ]),
    ("Rodapé e redes", [
        ("rodape_texto", "Frase do rodapé", "text", "IA explicada em português, para quem quer usar e não só ouvir falar."),
        ("instagram", "Link do Instagram", "url", ""),
        ("youtube", "Link do YouTube", "url", ""),
        ("email_contato", "E-mail de contato", "text", ""),
        ("seo_descricao", "Descrição para o Google (até 160 caracteres)", "textarea",
         "MentesIA: inteligência artificial sem mistério. Novidades, materiais gratuitos e cursos práticos sobre IA."),
    ]),
]
SETTING_DEFAULTS = {k: d for _, fields in SETTINGS for k, _, _, d in fields}


def get_settings():
    rows = db().execute("SELECT chave, valor FROM config").fetchall()
    data = dict(SETTING_DEFAULTS)
    data.update({r["chave"]: r["valor"] for r in rows})
    return data


def parse_faq(text):
    items = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = block.strip().split("\n", 1)
        if lines and lines[0].strip():
            items.append({"q": lines[0].strip(), "a": lines[1].strip() if len(lines) > 1 else ""})
    return items


# =====================================================================
# Definição dos conteúdos editáveis (gera listas e formulários do painel)
# tipo: text | textarea | markdown | lines | int | money | url | bool | image | file | slug
# =====================================================================
ENTITIES = {
    "artigos": {
        "table": "posts", "label": "Artigos", "singular": "artigo", "title_field": "titulo",
        "cols": [("categoria", "Categoria"), ("tempo", "Leitura (min)")],
        "fields": [
            ("titulo", "Título", "text", {"required": True}),
            ("resumo", "Resumo (aparece no card)", "textarea", {}),
            ("conteudo", "Texto do artigo", "markdown", {"help": "Use **negrito**, ## Subtítulo, - listas e [link](https://...)."}),
            ("categoria", "Categoria", "text", {"side": True, "datalist": "categorias", "help": "Vira filtro na home."}),
            ("tempo", "Tempo de leitura (min)", "int", {"side": True}),
            ("slug", "Endereço da página", "slug", {"side": True, "help": "Gerado a partir do título se ficar vazio."}),
            ("capa", "Imagem de capa", "image", {"side": True}),
            ("destaque", "Destacar na home (card grande)", "bool", {"side": True}),
        ],
    },
    "materiais": {
        "table": "materiais", "label": "Materiais grátis", "singular": "material", "title_field": "titulo",
        "cols": [("formato", "Formato")],
        "fields": [
            ("titulo", "Título", "text", {"required": True}),
            ("descricao", "Descrição", "textarea", {}),
            ("itens", "Tópicos (um por linha)", "lines", {}),
            ("link", "Link de acesso", "url", {"help": "Ex.: produto gratuito na Hotmart. Se enviar um arquivo abaixo, o botão baixa o arquivo."}),
            ("arquivo", "Ou envie o arquivo (PDF, planilha…)", "file", {}),
            ("formato", "Formato", "text", {"side": True, "help": "Ex.: PDF · 24 páginas"}),
            ("botao", "Texto do botão", "text", {"side": True}),
        ],
    },
    "produtos": {
        "table": "produtos", "label": "Produtos (Hotmart)", "singular": "produto", "title_field": "titulo",
        "cols": [("tipo", "Tipo"), ("preco", "Preço")],
        "fields": [
            ("titulo", "Nome do produto", "text", {"required": True}),
            ("descricao", "Descrição", "textarea", {}),
            ("itens", "Benefícios (um por linha)", "lines", {}),
            ("link", "Link de checkout da Hotmart", "url", {"required": True, "help": "Ex.: https://pay.hotmart.com/XXXXXXXX"}),
            ("tipo", "Tipo", "text", {"side": True, "help": "Ex.: Curso em vídeo"}),
            ("preco", "Preço (R$)", "money", {"side": True}),
            ("preco_antigo", "Preço antigo riscado (R$)", "money", {"side": True, "help": "Deixe vazio para não mostrar."}),
            ("parcelas", "Parcelas no cartão", "int", {"side": True}),
            ("selo", "Selo (ex.: Mais vendido)", "text", {"side": True}),
            ("botao", "Texto do botão", "text", {"side": True}),
            ("capa_texto", "Texto da capa", "textarea", {"side": True, "help": "Usado quando não há imagem."}),
            ("imagem", "Imagem de capa", "image", {"side": True}),
        ],
    },
    "prompts": {
        "table": "prompts", "label": "Prompts do dia", "singular": "prompt", "title_field": "texto",
        "cols": [("tag", "Tag")],
        "fields": [
            ("texto", "Prompt", "textarea", {"required": True}),
            ("tag", "Tag", "text", {"side": True, "help": "Ex.: #produtividade"}),
        ],
    },
}


# =====================================================================
# Dados iniciais (exemplos — edite ou apague pelo painel)
# =====================================================================
def seed():
    c = db()
    if c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
        posts = [
            ("Guias", "Como escolher o assistente de IA certo para o seu trabalho",
             "ChatGPT, Claude, Gemini ou Copilot: um roteiro para decidir pelo tipo de tarefa que você faz todo dia, e não pelo hype.", 8, 1),
            ("Prompts", "5 prompts para resumir reuniões longas", "Transforme transcrições em ata, decisões e próximos passos.", 4, 0),
            ("Negócios", "IA no atendimento sem perder o tom da marca", "O que automatizar e o que manter humano.", 6, 0),
            ("Ferramentas", "Planilhas com IA: fórmulas escritas em português", "Descreva o cálculo e deixe a IA montar a fórmula.", 5, 0),
            ("Tendências", "Agentes de IA: o que são e o que já fazem", "Da conversa à execução de tarefas inteiras.", 7, 0),
            ("Guias", "Privacidade: o que nunca colar numa IA", "Dados de clientes, senhas e documentos internos.", 5, 0),
        ]
        for i, (cat, tit, res, tempo, dest) in enumerate(posts):
            corpo = (f"{res}\n\n## Texto de exemplo\n\nEste é um artigo de exemplo criado junto com o site. "
                     "Edite ou apague pelo painel em **/admin → Artigos**.\n\n- Tópico um\n- Tópico dois\n")
            c.execute("INSERT INTO posts (titulo, slug, categoria, resumo, conteudo, tempo, destaque, ativo, ordem, criado_em, atualizado_em)"
                      " VALUES (?,?,?,?,?,?,?,1,?,?,?)", (tit, slugify(tit), cat, res, corpo, tempo, dest, i, now(), now()))
    if c.execute("SELECT COUNT(*) FROM materiais").fetchone()[0] == 0:
        for i, (fmt, tit, desc, itens) in enumerate([
            ("PDF · 24 páginas", "Guia do primeiro prompt", "Como pedir o que você quer para uma IA e receber uma resposta útil na primeira tentativa.", "Estrutura de um bom prompt\n10 exemplos antes e depois"),
            ("Checklist", "IA na sua empresa", "20 tarefas do dia a dia que você já pode delegar para a IA, com a ferramenta indicada para cada uma.", "Atendimento, vendas e finanças\nFerramentas gratuitas"),
            ("Planilha", "Mapa de ferramentas de IA", "Comparativo das principais ferramentas: o que cada uma faz bem, quanto custa e para quem serve.", "Texto, imagem, vídeo e áudio\nFiltros por uso e preço"),
        ]):
            c.execute("INSERT INTO materiais (titulo, formato, descricao, itens, link, ativo, ordem, criado_em, atualizado_em) VALUES (?,?,?,?,?,1,?,?,?)",
                      (tit, fmt, desc, itens, "", i, now(), now()))
    if c.execute("SELECT COUNT(*) FROM produtos").fetchone()[0] == 0:
        for i, (tit, tipo, capa, desc, itens, preco, antigo, parc, selo) in enumerate([
            ("IA no Trabalho", "Curso em vídeo", "IA no\nTrabalho", "Automatize relatórios, e-mails e planilhas com ChatGPT, Claude e Gemini.",
             "Aulas gravadas e práticas\nModelos de prompts prontos\nCertificado de conclusão", 197, 297, 12, "Mais vendido"),
            ("Biblioteca de Prompts", "E-book + planilha", "300\nprompts", "Prompts testados e organizados por área: vendas, RH, finanças, marketing e estudo.",
             "Organizado por área\nPlanilha pesquisável\nAtualizações incluídas", 47, None, 5, ""),
            ("IA para Pequenos Negócios", "Mentoria em grupo", "IA para\nnegócios", "Encontros ao vivo para montar o plano de IA da sua empresa, do atendimento às vendas.",
             "Encontros ao vivo\nPlano de ação personalizado\nGrupo de alunos", 497, None, 12, ""),
        ]):
            c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, descricao, itens, preco, preco_antigo, parcelas, selo, link, ativo, ordem, criado_em, atualizado_em)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)", (tit, tipo, capa, desc, itens, preco, antigo, parc, selo, "", i, now(), now()))
    if c.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0:
        for i, (tag, txt) in enumerate([
            ("#produtividade", "Aja como meu assistente executivo. Vou colar minha lista de tarefas da semana. Organize por urgência e impacto, sugira o que posso delegar e monte um plano dia a dia."),
            ("#email", "Reescreva este e-mail para ficar claro, cordial e com no máximo 5 linhas. Mantenha o pedido principal na primeira frase: [cole o e-mail]"),
            ("#estudo", "Explique [tema] como se eu fosse iniciante. Use uma analogia do dia a dia e termine com 3 perguntas para eu testar se entendi."),
            ("#negócios", "Liste 10 tarefas repetitivas de uma [tipo de empresa] que podem ser feitas com IA hoje. Para cada uma, diga a ferramenta e o tempo economizado por semana."),
        ]):
            c.execute("INSERT INTO prompts (texto, tag, ativo, ordem, criado_em, atualizado_em) VALUES (?,?,1,?,?,?)", (txt, tag, i, now(), now()))
    c.commit()


with app.app_context():
    db().executescript(SCHEMA)
    seed()


# =====================================================================
# Utilidades de template
# =====================================================================
@app.template_filter("brl")
def brl(v):
    if v is None or v == "":
        return ""
    v = float(v)
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + (s[:-3] if s.endswith(",00") else s)


@app.template_filter("lines")
def lines(v):
    return [x.strip() for x in (v or "").splitlines() if x.strip()]


@app.template_filter("markdown")
def render_md(v):
    return Markup(md.markdown(v or "", extensions=["extra", "sane_lists", "nl2br"]))


def media_url(path):
    """Converte o valor salvo (URL externa ou arquivo enviado) em endereço público."""
    if not path:
        return ""
    if path.startswith(("http://", "https://", "/")):
        return path
    return url_for("uploads", filename=path)


app.jinja_env.globals["media_url"] = media_url


@app.context_processor
def inject():
    return {"S": get_settings() if request.endpoint not in ("static", "uploads") else {},
            "csrf_token": csrf_token, "ano": datetime.now().year}


# =====================================================================
# Segurança: CSRF, login e limite de tentativas
# =====================================================================
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST" and request.path.startswith("/admin"):
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("_csrf"):
            abort(400, "Sessão expirada. Volte e tente de novo.")


_attempts = {}


def too_many(key, limit, window):
    t = time.time()
    hits = [x for x in _attempts.get(key, []) if t - x < window]
    _attempts[key] = hits
    if len(hits) >= limit:
        return True
    hits.append(t)
    return False


def client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*a, **kw)
    return wrapper


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


# =====================================================================
# Site público
# =====================================================================
@app.route("/")
def home():
    c = db()
    posts = c.execute("SELECT * FROM posts WHERE ativo=1 ORDER BY destaque DESC, ordem, criado_em DESC").fetchall()
    materiais = c.execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY ordem, id").fetchall()
    produtos = c.execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY ordem, id").fetchall()
    prompts = [dict(texto=r["texto"], tag=r["tag"]) for r in
               c.execute("SELECT texto, tag FROM prompts WHERE ativo=1 ORDER BY ordem, id").fetchall()]
    categorias = []
    for p in posts:
        if p["categoria"] and p["categoria"] not in categorias:
            categorias.append(p["categoria"])
    return render_template("home.html", posts=posts, materiais=materiais, produtos=produtos,
                           prompts_json=json.dumps(prompts, ensure_ascii=False), categorias=categorias,
                           faq=parse_faq(get_settings().get("faq")))


@app.route("/blog/<slug>")
def post(slug):
    p = db().execute("SELECT * FROM posts WHERE slug=? AND (ativo=1 OR ?)", (slug, 1 if session.get("admin") else 0)).fetchone()
    if not p:
        abort(404)
    outros = db().execute("SELECT * FROM posts WHERE ativo=1 AND id<>? ORDER BY criado_em DESC LIMIT 3", (p["id"],)).fetchall()
    return render_template("post.html", p=p, outros=outros)


@app.route("/material/<int:mid>")
def material(mid):
    m = db().execute("SELECT * FROM materiais WHERE id=? AND ativo=1", (mid,)).fetchone()
    if not m:
        abort(404)
    if m["arquivo"]:
        return send_from_directory(UPLOAD_DIR, m["arquivo"], as_attachment=True)
    if m["link"]:
        return redirect(m["link"])
    abort(404)


@app.route("/uploads/<path:filename>")
def uploads(filename):
    resp = send_from_directory(UPLOAD_DIR, filename)
    # SVG pode conter scripts: servir isolado
    resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
    return resp


@app.route("/api/newsletter", methods=["POST"])
def newsletter():
    data = request.get_json(silent=True) or request.form
    if data.get("site"):  # campo-armadilha contra robôs
        return jsonify(ok=True)
    email = (data.get("email") or "").strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", email) or len(email) > 200:
        return jsonify(ok=False, erro="Digite um e-mail válido, como nome@email.com."), 400
    if too_many("news:" + client_ip(), 5, 600):
        return jsonify(ok=False, erro="Muitas tentativas. Tente de novo em alguns minutos."), 429
    db().execute("INSERT OR IGNORE INTO inscritos (email, origem, criado_em) VALUES (?,?,?)",
                 (email, (data.get("origem") or "site")[:40], now()))
    db().commit()
    return jsonify(ok=True, mensagem=get_settings()["news_sucesso"])


@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# =====================================================================
# Painel administrativo
# =====================================================================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    erro = None
    if request.method == "POST":
        if too_many("login:" + client_ip(), 5, 600):
            erro = "Muitas tentativas. Aguarde 10 minutos."
        elif not ADMIN_PASSWORD_HASH:
            erro = "Senha do painel não configurada. Defina ADMIN_PASSWORD no arquivo .env."
        elif (request.form.get("usuario", "").strip() == ADMIN_USER
              and check_password_hash(ADMIN_PASSWORD_HASH, request.form.get("senha", ""))):
            session.clear()
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_home"))
        else:
            erro = "Usuário ou senha incorretos."
    return render_template("admin/login.html", erro=erro)


@app.route("/admin/sair", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@login_required
def admin_home():
    c = db()
    contagem = {k: c.execute(f"SELECT COUNT(*) FROM {e['table']}").fetchone()[0] for k, e in ENTITIES.items()}
    ativos = {k: c.execute(f"SELECT COUNT(*) FROM {e['table']} WHERE ativo=1").fetchone()[0] for k, e in ENTITIES.items()}
    inscritos = c.execute("SELECT COUNT(*) FROM inscritos").fetchone()[0]
    semana = c.execute("SELECT COUNT(*) FROM inscritos WHERE criado_em >= datetime('now','-7 days','localtime')").fetchone()[0]
    recentes = c.execute("SELECT id, titulo, atualizado_em, ativo FROM posts ORDER BY atualizado_em DESC LIMIT 5").fetchall()
    sem_link = c.execute("SELECT id, titulo FROM produtos WHERE ativo=1 AND (link='' OR link LIKE '%SEU-CODIGO%')").fetchall()
    return render_template("admin/home.html", contagem=contagem, ativos=ativos, inscritos=inscritos,
                           semana=semana, recentes=recentes, sem_link=sem_link, entities=ENTITIES)


def get_entity(kind):
    ent = ENTITIES.get(kind)
    if not ent:
        abort(404)
    return ent


@app.route("/admin/<kind>")
@login_required
def admin_list(kind):
    ent = get_entity(kind)
    q = request.args.get("q", "").strip()
    sql = f"SELECT * FROM {ent['table']}"
    args = ()
    if q:
        sql += f" WHERE {ent['title_field']} LIKE ?"
        args = (f"%{q}%",)
    rows = db().execute(sql + " ORDER BY ordem, id", args).fetchall()
    return render_template("admin/list.html", kind=kind, ent=ent, rows=rows, q=q, entities=ENTITIES)


def save_upload(fs, allowed):
    if not fs or not fs.filename:
        return None
    ext = fs.filename.rsplit(".", 1)[-1].lower() if "." in fs.filename else ""
    if ext not in allowed:
        raise ValueError(f"Tipo de arquivo não permitido (.{ext}). Use: {', '.join(sorted(allowed))}.")
    name = f"{uuid.uuid4().hex[:8]}-{secure_filename(fs.filename)}"
    fs.save(os.path.join(UPLOAD_DIR, name))
    return name


def to_number(v, kind):
    v = (v or "").strip()
    if not v:
        return None
    try:
        if kind == "int":
            return int(v)
        return float(v.replace("R$", "").replace(".", "").replace(",", ".") if "," in v else v)
    except ValueError:
        raise ValueError(f"“{v}” não é um número válido.")


@app.route("/admin/<kind>/novo", methods=["GET", "POST"])
@app.route("/admin/<kind>/<int:item_id>", methods=["GET", "POST"])
@login_required
def admin_edit(kind, item_id=None):
    ent = get_entity(kind)
    c = db()
    row = None
    if item_id:
        row = c.execute(f"SELECT * FROM {ent['table']} WHERE id=?", (item_id,)).fetchone()
        if not row:
            abort(404)
    values = dict(row) if row else {"ativo": 1, "ordem": 0}
    erros = []

    if request.method == "POST":
        form = request.form
        for name, label, ftype, opts in ent["fields"]:
            if ftype == "bool":
                values[name] = 1 if form.get(name) else 0
            elif ftype in ("image", "file"):
                try:
                    up = save_upload(request.files.get(name + "_upload"), IMAGE_EXT if ftype == "image" else FILE_EXT)
                except ValueError as e:
                    erros.append(f"{label}: {e}")
                    up = None
                if form.get(name + "_remover"):
                    values[name] = ""
                elif up:
                    values[name] = up
                else:
                    values[name] = (form.get(name) or "").strip()
            elif ftype in ("int", "money"):
                try:
                    values[name] = to_number(form.get(name), ftype)
                except ValueError as e:
                    erros.append(f"{label}: {e}")
            else:
                values[name] = (form.get(name) or "").strip()
            if opts.get("required") and not values.get(name):
                erros.append(f"Preencha o campo “{label}”.")
        values["ativo"] = 1 if form.get("ativo") else 0
        try:
            values["ordem"] = int(form.get("ordem") or 0)
        except ValueError:
            values["ordem"] = 0

        if kind == "artigos":
            values["slug"] = slugify(values.get("slug") or values.get("titulo") or "")
            dup = c.execute("SELECT id FROM posts WHERE slug=? AND id<>?", (values["slug"], item_id or 0)).fetchone()
            if dup:
                values["slug"] = f"{values['slug']}-{uuid.uuid4().hex[:4]}"
            if values.get("tempo") is None:
                values["tempo"] = max(1, round(len((values.get("conteudo") or "").split()) / 200))
            if values.get("destaque"):
                c.execute("UPDATE posts SET destaque=0 WHERE id<>?", (item_id or 0,))

        if not erros:
            cols = [f[0] for f in ent["fields"]] + ["ativo", "ordem"]
            data = [values.get(k) for k in cols]
            if row:
                c.execute(f"UPDATE {ent['table']} SET {', '.join(k + '=?' for k in cols)}, atualizado_em=? WHERE id=?",
                          data + [now(), item_id])
            else:
                cur = c.execute(f"INSERT INTO {ent['table']} ({', '.join(cols)}, criado_em, atualizado_em) VALUES "
                                f"({', '.join('?' * len(cols))}, ?, ?)", data + [now(), now()])
                item_id = cur.lastrowid
            c.commit()
            flash(f"{ent['singular'].capitalize()} salvo.", "ok")
            if form.get("acao") == "salvar_novo":
                return redirect(url_for("admin_edit", kind=kind))
            return redirect(url_for("admin_edit", kind=kind, item_id=item_id))

    categorias = [r[0] for r in c.execute("SELECT DISTINCT categoria FROM posts WHERE categoria<>'' ORDER BY 1")]
    return render_template("admin/form.html", kind=kind, ent=ent, v=values, item_id=item_id, erros=erros,
                           categorias=categorias, entities=ENTITIES)


@app.route("/admin/<kind>/<int:item_id>/excluir", methods=["POST"])
@login_required
def admin_delete(kind, item_id):
    ent = get_entity(kind)
    db().execute(f"DELETE FROM {ent['table']} WHERE id=?", (item_id,))
    db().commit()
    flash(f"{ent['singular'].capitalize()} excluído.", "ok")
    return redirect(url_for("admin_list", kind=kind))


@app.route("/admin/<kind>/<int:item_id>/alternar", methods=["POST"])
@login_required
def admin_toggle(kind, item_id):
    ent = get_entity(kind)
    db().execute(f"UPDATE {ent['table']} SET ativo = 1 - ativo, atualizado_em=? WHERE id=?", (now(), item_id))
    db().commit()
    return redirect(request.referrer or url_for("admin_list", kind=kind))


@app.route("/admin/<kind>/ordenar", methods=["POST"])
@login_required
def admin_reorder(kind):
    ent = get_entity(kind)
    ids = [int(x) for x in request.form.get("ids", "").split(",") if x.isdigit()]
    for i, item in enumerate(ids):
        db().execute(f"UPDATE {ent['table']} SET ordem=? WHERE id=?", (i, item))
    db().commit()
    return jsonify(ok=True)


@app.route("/admin/preview", methods=["POST"])
@login_required
def admin_preview():
    return jsonify(html=str(render_md(request.form.get("texto", ""))))


@app.route("/admin/textos", methods=["GET", "POST"])
@login_required
def admin_settings():
    if request.method == "POST":
        c = db()
        for _, fields in SETTINGS:
            for key, *_ in fields:
                if key in request.form:
                    c.execute("INSERT INTO config (chave, valor) VALUES (?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                              (key, request.form[key].strip()))
        c.commit()
        flash("Textos do site salvos.", "ok")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", groups=SETTINGS, valores=get_settings(), entities=ENTITIES)


@app.route("/admin/inscritos")
@login_required
def admin_subscribers():
    rows = db().execute("SELECT * FROM inscritos ORDER BY criado_em DESC").fetchall()
    return render_template("admin/subscribers.html", rows=rows, entities=ENTITIES)


@app.route("/admin/inscritos.csv")
@login_required
def admin_subscribers_csv():
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["email", "origem", "data"])
    for r in db().execute("SELECT email, origem, criado_em FROM inscritos ORDER BY criado_em"):
        w.writerow(list(r))
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=inscritos-mentesia.csv"})


@app.route("/admin/inscritos/<int:sid>/excluir", methods=["POST"])
@login_required
def admin_subscriber_delete(sid):
    db().execute("DELETE FROM inscritos WHERE id=?", (sid,))
    db().commit()
    flash("Inscrito removido.", "ok")
    return redirect(url_for("admin_subscribers"))


@app.route("/admin/midia", methods=["GET", "POST"])
@login_required
def admin_media():
    if request.method == "POST":
        enviados = 0
        for fs in request.files.getlist("arquivos"):
            try:
                if save_upload(fs, FILE_EXT):
                    enviados += 1
            except ValueError as e:
                flash(str(e), "erro")
        if enviados:
            flash(f"{enviados} arquivo(s) enviado(s).", "ok")
        return redirect(url_for("admin_media"))
    files = []
    for name in sorted(os.listdir(UPLOAD_DIR), key=lambda n: -os.path.getmtime(os.path.join(UPLOAD_DIR, n))):
        p = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(p):
            files.append({"nome": name, "kb": max(1, os.path.getsize(p) // 1024),
                          "imagem": name.rsplit(".", 1)[-1].lower() in IMAGE_EXT,
                          "url": url_for("uploads", filename=name, _external=True)})
    return render_template("admin/media.html", files=files, entities=ENTITIES)


@app.route("/admin/midia/<path:name>/excluir", methods=["POST"])
@login_required
def admin_media_delete(name):
    p = os.path.join(UPLOAD_DIR, secure_filename(name))
    if os.path.isfile(p):
        os.remove(p)
        flash("Arquivo excluído.", "ok")
    return redirect(url_for("admin_media"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", port=int(os.environ.get("PORT", 5000)))
