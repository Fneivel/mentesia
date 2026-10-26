"""
Mentes.IA — site + painel administrativo.

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
from zoneinfo import ZoneInfo
from functools import wraps
from urllib.parse import urlparse

import markdown as md

import acessos
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
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

# Pixel da Meta (anúncios do Instagram/Facebook). Deixe META_PIXEL_ID vazio no Render para desligar.
META_PIXEL_ID = "".join(ch for ch in os.environ.get("META_PIXEL_ID", "1835420714682410") if ch.isdigit())

# Ofertas por tempo limitado: datas digitadas no painel são horário de Brasília
FUSO = ZoneInfo("America/Sao_Paulo")

# Caminhos por objetivo: bloco "O que você quer fazer?" da home, páginas /caminhos/<slug> e filtro da loja.
# Cada material, artigo e produto é marcado no painel com um ou mais destes objetivos.
OBJETIVOS = [
    {"slug": "comecar", "titulo": "Começar do zero", "curto": "Começar do zero",
     "texto": "Entenda o básico e faça seus primeiros pedidos para a IA, sem jargão."},
    {"slug": "produtividade", "titulo": "Ganhar produtividade no trabalho", "curto": "Produtividade",
     "texto": "Delegue relatórios, e-mails, planilhas e reuniões para a IA."},
    {"slug": "conteudo", "titulo": "Criar conteúdo", "curto": "Criar conteúdo",
     "texto": "Posts, roteiros, legendas e calendário editorial com ajuda da IA."},
    {"slug": "tecnico", "titulo": "Aprofundar em ferramentas técnicas", "curto": "Técnico",
     "texto": "Dados, código, agentes e plataformas como Databricks e Copilot Studio."},
]
OBJ_POR_SLUG = {o["slug"]: o for o in OBJETIVOS}

IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif", "svg"}
FILE_EXT = IMAGE_EXT | {"pdf", "zip", "xlsx", "docx", "pptx", "csv", "txt"}

app = Flask(__name__)
# O Render entrega o site por HTTPS na frente do app: assim os links completos saem com https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
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
  botao TEXT DEFAULT 'Comprar na Hotmart', pagina TEXT DEFAULT '',
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
         "Conteúdo em português\nMateriais 100% gratuitos\nCompra segura"),
    ]),
    ("Seção de novidades", [
        ("blog_titulo", "Título", "text", "Novidades e guias"),
        ("blog_texto", "Texto", "textarea", "O que muda com cada ferramenta nova, explicado sem jargão e com exemplo de uso."),
    ]),
    ("Seção de materiais gratuitos", [
        ("gratis_titulo", "Título", "text", "Comece sem gastar nada"),
        ("gratis_texto", "Texto", "textarea", "Guias, checklists e planilhas para dar os primeiros passos. Sem cadastro: clique e o arquivo é salvo no seu aparelho."),
    ]),
    ("Caminhos por objetivo", [
        ("caminhos_titulo", "Título do bloco na home", "text", "O que você quer fazer com IA?"),
        ("caminhos_texto", "Texto", "textarea", "Escolha um caminho e veja só os materiais, artigos e produtos que fazem sentido para você."),
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
         "Os materiais gratuitos são mesmo gratuitos?\nSão. Não pedimos cadastro nem cartão: ao tocar em “Baixar”, o arquivo é salvo direto no seu aparelho, na pasta Downloads."),
    ]),
    ("Rodapé e redes", [
        ("rodape_texto", "Frase do rodapé", "text", "IA explicada em português, para quem quer usar e não só ouvir falar."),
        ("instagram", "Link do Instagram", "url", ""),
        ("youtube", "Link do YouTube", "url", ""),
        ("email_contato", "E-mail de contato", "text", ""),
        ("seo_descricao", "Descrição para o Google (até 160 caracteres)", "textarea",
         "Mentes.IA: inteligência artificial sem mistério. Novidades, materiais gratuitos e cursos práticos sobre IA."),
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
        "cols": [("objetivos", "Classificação"), ("categoria", "Categoria"), ("tempo", "Leitura (min)")],
        "fields": [
            ("titulo", "Título", "text", {"required": True}),
            ("objetivos", "Classificação", "objetivos", {"required": True, "help": "Obrigatório. Marque um ou mais caminhos: é o que faz este conteúdo aparecer em “O que você quer fazer com IA?” na home."}),
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
        "cols": [("objetivos", "Classificação"), ("formato", "Formato")],
        "fields": [
            ("titulo", "Título", "text", {"required": True}),
            ("objetivos", "Classificação", "objetivos", {"required": True, "help": "Obrigatório. Marque um ou mais caminhos: é o que faz este conteúdo aparecer em “O que você quer fazer com IA?” na home."}),
            ("descricao", "Descrição", "textarea", {}),
            ("itens", "Tópicos (um por linha)", "lines", {}),
            ("link", "Link de acesso", "url", {"help": "Ex.: produto gratuito na Hotmart. Se enviar um arquivo abaixo, o botão baixa o arquivo."}),
            ("arquivo", "Ou envie o arquivo (PDF, planilha…)", "file", {}),
            ("conteudo", "Texto da página do material (opcional)", "markdown", {"help": "Aparece na página própria do material, abaixo dos tópicos. Use **negrito**, ## Subtítulo e - listas."}),
            ("formato", "Formato", "text", {"side": True, "help": "Ex.: PDF · 24 páginas"}),
            ("botao", "Texto do botão", "text", {"side": True}),
            ("slug", "Endereço da página", "slug", {"side": True, "help": "Fica mentesia.com.br/materiais-gratis/endereco. Gerado a partir do título se ficar vazio. Evite mudar depois de divulgar o link."}),
            ("capa", "Imagem de capa", "image", {"side": True, "help": "Aparece na página e na prévia do link no WhatsApp e redes. Ideal: 1200×630."}),
            ("miniatura", "Miniatura do card no celular", "image", {"side": True, "help": "Quadrada, 400×400, com título curto e letras grandes. Sem ela, o card mostra o ícone da marca com o formato."}),
        ],
    },
    "produtos": {
        "table": "produtos", "label": "Produtos (Hotmart)", "singular": "produto", "title_field": "titulo",
        "cols": [("objetivos", "Classificação"), ("tipo", "Tipo"), ("preco", "Preço")],
        "fields": [
            ("titulo", "Nome do produto", "text", {"required": True}),
            ("objetivos", "Classificação", "objetivos", {"required": True, "help": "Obrigatório. Marque um ou mais caminhos: é o que faz este conteúdo aparecer em “O que você quer fazer com IA?” na home e nos filtros da loja."}),
            ("descricao", "Descrição", "textarea", {}),
            ("itens", "Benefícios (um por linha)", "lines", {}),
            ("link", "Link de checkout da Hotmart", "url", {"help": "Ex.: https://pay.hotmart.com/XXXXXXXX. Enquanto estiver vazio, o botão de compra aparece como “Em breve”."}),
            ("pagina", "Página de vendas no site", "text", {"side": True, "help": "Ex.: guia-copilot. Se preenchido, o card da loja leva para mentesia.com.br/produtos/guia-copilot. Deixe vazio para o botão ir direto à Hotmart."}),
            ("tipo", "Tipo", "text", {"side": True, "help": "Ex.: Curso em vídeo"}),
            ("compatibilidade", "Compatibilidade / requisito", "text", {"side": True, "help": "Aparece junto do preço e do botão. Ex.: Somente Windows 10 e 11. Deixe vazio se funciona em qualquer aparelho."}),
            ("preco", "Preço (R$)", "money", {"side": True}),
            ("preco_antigo", "Preço antigo riscado (R$)", "money", {"side": True, "help": "Deixe vazio para não mostrar."}),
            ("parcelas", "Parcelas no cartão", "int", {"side": True}),
            ("selo", "Selo (ex.: Mais vendido)", "text", {"side": True}),
            ("botao", "Texto do botão", "text", {"side": True}),
            ("capa_texto", "Texto da capa", "textarea", {"side": True, "help": "Usado quando não há imagem."}),
            ("imagem", "Imagem de capa", "image", {"side": True, "help": "Formato largo, 1200×750."}),
            ("miniatura", "Miniatura do card no celular", "image", {"side": True, "help": "Quadrada, 400×400, com título curto. Sem ela, o card mostra o texto da capa."}),
            ("oferta_ativa", "Ativar oferta com contador", "bool", {
                "secao": "Oferta por tempo limitado",
                "secao_help": "Mostra preço promocional e contagem regressiva na loja e na página do produto. "
                              "Quando o prazo acaba, o site volta sozinho para o preço e o link normais. "
                              "Na Hotmart, crie um cupom com a MESMA data de fim, para o desconto expirar lá também."}),
            ("oferta_preco", "Preço da oferta (R$)", "money", {"help": "O preço normal acima aparece riscado durante a oferta."}),
            ("oferta_fim", "Oferta termina em", "datetime", {"help": "Horário de Brasília. Use a mesma data e hora de fim do cupom na Hotmart."}),
            ("oferta_link", "Link do checkout com o cupom", "url", {"help": "Na Hotmart, copie o link do cupom (checkout já com o desconto aplicado). Se ficar vazio, o botão usa o link normal."}),
            ("oferta_texto", "Chamada da oferta", "text", {"help": "Ex.: Preço de lançamento · Black Friday · Oferta por tempo limitado. Vazio = “Oferta por tempo limitado”."}),
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


def migrate():
    """Ajustes de estrutura em bancos já existentes (rodam a cada início, sem perder dados)."""
    c = db()
    cols = [r[1] for r in c.execute("PRAGMA table_info(produtos)")]
    if "pagina" not in cols:
        c.execute("ALTER TABLE produtos ADD COLUMN pagina TEXT DEFAULT ''")
    for col, tipo in (("oferta_ativa", "INTEGER DEFAULT 0"), ("oferta_preco", "REAL"), ("oferta_fim", "TEXT DEFAULT ''"),
                      ("oferta_link", "TEXT DEFAULT ''"), ("oferta_texto", "TEXT DEFAULT ''")):
        if col not in cols:
            c.execute(f"ALTER TABLE produtos ADD COLUMN {col} {tipo}")
    for col in ("compatibilidade", "miniatura", "objetivos"):
        if col not in cols:
            c.execute(f"ALTER TABLE produtos ADD COLUMN {col} TEXT DEFAULT ''")
    pcols = [r[1] for r in c.execute("PRAGMA table_info(posts)")]
    if "objetivos" not in pcols:
        c.execute("ALTER TABLE posts ADD COLUMN objetivos TEXT DEFAULT ''")
    mcols = [r[1] for r in c.execute("PRAGMA table_info(materiais)")]
    for col in ("slug", "conteudo", "capa", "miniatura", "objetivos"):
        if col not in mcols:
            c.execute(f"ALTER TABLE materiais ADD COLUMN {col} TEXT DEFAULT ''")
    usados = {r[0] for r in c.execute("SELECT slug FROM materiais WHERE slug<>''")}
    for r in c.execute("SELECT id, titulo FROM materiais WHERE slug IS NULL OR slug=''").fetchall():
        c.execute("UPDATE materiais SET slug=? WHERE id=?", (slug_livre(r["titulo"], usados), r["id"]))
    c.commit()


def slug_livre(texto, usados):
    """Endereço a partir do texto, sem repetir um que já está em uso."""
    base = slugify(texto) or "material"
    slug, n = base, 2
    while slug in usados:
        slug, n = f"{base}-{n}", n + 1
    usados.add(slug)
    return slug


def seed_once():
    """Conteúdos novos adicionados uma única vez (se você apagar pelo painel, não voltam)."""
    c = db()
    if not c.execute("SELECT 1 FROM config WHERE chave='seed_guia_copilot_v3'").fetchone():
        c.execute("UPDATE produtos SET ordem = ordem + 1")
        c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, imagem, descricao, itens, preco, preco_antigo, parcelas, selo, link, botao, pagina, ativo, ordem, criado_em, atualizado_em)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                  ("Guia Definitivo do Copilot 3.0", "Guia digital + Study Hub", "Guia\nCopilot 3.0",
                   "/static/img/produtos/guia-copilot-v3.webp",
                   "Microsoft 365 Copilot e Copilot Studio do básico aos agentes, atualizado com as novidades de 2025 e 2026.",
                   "16 partes e 71 páginas\n13 fluxos de agentes ilustrados\nStudy Hub com quiz e flashcards",
                   0, None, 12, "Novo · v3.0", "", "Quero o guia", "guia-copilot", now(), now()))
        c.execute("INSERT INTO config (chave, valor) VALUES ('seed_guia_copilot_v3', '1')")
        c.commit()
    if not c.execute("SELECT 1 FROM config WHERE chave='seed_social_hub'").fetchone():
        # Entra logo depois do primeiro produto da vitrine. Preço e link da Hotmart: preencher no painel.
        c.execute("UPDATE produtos SET ordem = ordem + 1 WHERE ordem >= 1")
        c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, imagem, descricao, itens, preco, preco_antigo, parcelas, selo, link, botao, pagina, ativo, ordem, criado_em, atualizado_em)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
                  ("Mentes.IA Social Hub", "Programa para Windows", "Social\nHub",
                   "/static/img/produtos/social-hub.webp",
                   "Calendário editorial para organizar as postagens de todas as suas marcas, direto no seu computador e sem internet.",
                   "Calendário com 8 redes sociais\nWorkflow do rascunho ao publicado\nPython incluído · funciona offline",
                   0, None, 12, "Novo", "", "Quero o Social Hub", "social-hub", now(), now()))
        c.execute("INSERT INTO config (chave, valor) VALUES ('seed_social_hub', '1')")
        c.commit()
    if not c.execute("SELECT 1 FROM config WHERE chave='seed_guia_claude_v1'").fetchone():
        # Entra logo depois do guia do Copilot. Preço e link da Hotmart são preenchidos pelo painel;
        # enquanto o link estiver vazio, o botão aparece como "Em breve".
        c.execute("UPDATE produtos SET ordem = ordem + 1 WHERE ordem >= 1")
        c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, imagem, descricao, itens, preco, preco_antigo, parcelas, selo, link, botao, pagina, ativo, ordem, criado_em, atualizado_em)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)",
                  ("Guia Definitivo do Claude", "E-book + Study Hub", "Guia\nClaude",
                   "/static/img/produtos/guia-claude.webp",
                   "Do primeiro prompt aos sistemas automatizados, com tudo o que o Claude faz em 2026.",
                   "84 páginas em 10 capítulos\n50 prompts e 5 modelos de skill\nStudy Hub com busca e progresso",
                   0, None, 12, "Lançamento", "", "Quero o guia", "guia-claude", now(), now()))
        c.execute("INSERT INTO config (chave, valor) VALUES ('seed_guia_claude_v1', '1')")
        c.commit()
    if not c.execute("SELECT 1 FROM config WHERE chave='seed_biblioteca_skills_v1'").fetchone():
        # Entra no topo da vitrine como lançamento. Preço e link da Hotmart: preencher no painel.
        c.execute("UPDATE produtos SET ordem = ordem + 1")
        c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, imagem, descricao, itens, preco, preco_antigo, parcelas, selo, link, botao, pagina, ativo, ordem, criado_em, atualizado_em)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                  ("Biblioteca de Skills para Claude", "Skills + laboratório + manual", "Skills\nClaude",
                   "/static/img/produtos/biblioteca-skills.webp",
                   "36 skills em português que fazem o Claude trabalhar do seu jeito, organizadas por perfil.",
                   "36 skills em 4 pacotes\nSeletor de kit por perfil\nInstalador, ZIPs e manual em PDF",
                   0, None, 12, "Lançamento", "", "Quero a biblioteca", "biblioteca-skills", now(), now()))
        c.execute("INSERT INTO config (chave, valor) VALUES ('seed_biblioteca_skills_v1', '1')")
        c.commit()
    if not c.execute("SELECT 1 FROM config WHERE chave='seed_databricks_150_v1'").fetchone():
        # Entra no topo da vitrine como lançamento. Preço e link da Hotmart: preencher no painel
        # (enquanto o link estiver vazio, o botão aparece como "Em breve").
        c.execute("UPDATE produtos SET ordem = ordem + 1")
        c.execute("INSERT INTO produtos (titulo, tipo, capa_texto, imagem, descricao, itens, preco, preco_antigo, parcelas, selo, link, botao, pagina, ativo, ordem, criado_em, atualizado_em)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                  ("Databricks em 150 Mapas", "Guia visual em PDF + HTML", "Databricks\n150 mapas",
                   "/static/img/produtos/databricks-150-mapas.webp",
                   "Do primeiro notebook ao Lakehouse em produção, com a tela real da plataforma em cada mapa.",
                   "150 mapas em 11 módulos\n164 exemplos de código e 11 mini projetos\nPDF de 200 páginas + HTML interativo",
                   0, None, 12, "Lançamento", "", "Quero os 150 mapas", "databricks-150-mapas", now(), now()))
        c.execute("INSERT INTO config (chave, valor) VALUES ('seed_databricks_150_v1', '1')")
        c.commit()


# Palavras-chave para sugerir o caminho de conteúdos que ainda não foram marcados no painel
PISTAS_OBJETIVO = {
    "tecnico": r"databricks|python|c[óo]digo|github|\bsql\b|engenharia de dados|\bapi\b|agentes?\b|n8n|copilot studio|claude code|\bmcp\b|lakehouse|programa[çc][ãa]o",
    "conteudo": r"conte[úu]dos?\b|instagram|tiktok|\bposts?\b|redes sociais|roteiros?\b|legendas?\b|marketing|calend[áa]rio editorial|carross",
    "produtividade": r"trabalho|produtiv|excel|planilha|reuni[ãoõ]|e-?mails?\b|empresa|neg[óo]cio|rotina|atendimento|relat[óo]rio|escrit[óo]rio|delegar",
    "comecar": r"primeir[oa]s?\b|iniciante|come[çc]ar|b[áa]sico|o que [ée]|do zero|sem mist[ée]rio|passo a passo|privacidade",
}


def sugerir_objetivos(*textos):
    """No máximo dois caminhos por conteúdo, para o filtro continuar útil."""
    t = " ".join(str(x or "") for x in textos).lower()
    achados = [k for k, rx in PISTAS_OBJETIVO.items() if re.search(rx, t)]
    return ",".join(achados[:2] or ["comecar"])


def ajustes_usabilidade_v1():
    """Ajustes da análise de usabilidade (out/2026), aplicados uma única vez em bancos que já existem:
    marca Mentes.IA, promessa do download direto, formato padronizado, imagens WebP,
    miniaturas, compatibilidade e caminhos por objetivo. Tudo continua editável no painel."""
    c = db()
    if c.execute("SELECT 1 FROM config WHERE chave='ajuste_usabilidade_v1'").fetchone():
        return
    marca = lambda v: re.sub(r"Mentes ?IA(?!\w)", "Mentes.IA", v) if isinstance(v, str) else v
    # Textos do site já salvos no painel
    trocas = {
        "Você recebe o acesso no e-mail.": "Sem cadastro: clique e o arquivo é salvo no seu aparelho.",
        "Você só informa seu e-mail para receber o link de acesso.": "Não pedimos cadastro nem cartão: ao tocar em “Baixar”, o arquivo é salvo direto no seu aparelho, na pasta Downloads.",
    }
    for r in c.execute("SELECT chave, valor FROM config").fetchall():
        v = r["valor"] or ""
        novo = v
        for a, b in trocas.items():
            novo = novo.replace(a, b)
        if not novo.startswith(("http://", "https://")):
            novo = marca(novo)
        if novo != v:
            c.execute("UPDATE config SET valor=? WHERE chave=?", (novo, r["chave"]))
    # Nome da marca nos textos (nunca em links, arquivos ou endereços)
    campos = {"posts": ("titulo", "resumo", "conteudo", "categoria"),
              "materiais": ("titulo", "descricao", "itens", "conteudo", "formato", "botao"),
              "produtos": ("titulo", "tipo", "capa_texto", "descricao", "itens", "selo", "botao", "oferta_texto")}
    for tabela, cols in campos.items():
        for r in c.execute(f"SELECT id, {', '.join(cols)} FROM {tabela}").fetchall():
            novos = [marca(r[k]) for k in cols]
            if novos != [r[k] for k in cols]:
                c.execute(f"UPDATE {tabela} SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*novos, r["id"]))
    # Formato padronizado ("PDF · 35 páginas") e botão do download direto
    for r in c.execute("SELECT id, formato, botao FROM materiais").fetchall():
        formato = re.sub(r"\s+[-–—|]\s+", " · ", r["formato"] or "")
        botao = "" if (r["botao"] or "").strip() in ("Quero o material grátis", "Quero o material") else r["botao"]
        c.execute("UPDATE materiais SET formato=?, botao=? WHERE id=?", (formato, botao, r["id"]))
    # Produtos da casa: imagem leve (WebP), miniatura quadrada, compatibilidade e caminhos
    casa = {
        "guia-copilot": ("guia-copilot-v3", "produtividade,tecnico", ""),
        "guia-claude": ("guia-claude", "comecar,produtividade", ""),
        "biblioteca-skills": ("biblioteca-skills", "produtividade,conteudo", "Requer conta no Claude (app, Cowork ou Claude Code)"),
        "databricks-150-mapas": ("databricks-150-mapas", "tecnico", ""),
        "social-hub": ("social-hub", "conteudo", "Somente Windows 10 e 11 (64 bits)"),
    }
    for pagina, (img, objs, compat) in casa.items():
        r = c.execute("SELECT id, imagem, miniatura, objetivos, compatibilidade FROM produtos WHERE pagina=?", (pagina,)).fetchone()
        if not r:
            continue
        imagem = r["imagem"]
        if imagem == f"/static/img/produtos/{img}.png":
            imagem = f"/static/img/produtos/{img}.webp"
        c.execute("UPDATE produtos SET imagem=?, miniatura=?, objetivos=?, compatibilidade=? WHERE id=?",
                  (imagem, r["miniatura"] or f"/static/img/produtos/{pagina}/mini.webp",
                   r["objetivos"] or objs, r["compatibilidade"] or compat, r["id"]))
    # Sugestão de caminho para o que ainda não foi marcado (dá para corrigir no painel)
    for tabela, cols in (("materiais", "titulo, descricao, itens, formato"), ("posts", "titulo, resumo, categoria"),
                         ("produtos", "titulo, tipo, descricao, itens")):
        for r in c.execute(f"SELECT id, {cols} FROM {tabela} WHERE objetivos IS NULL OR objetivos=''").fetchall():
            c.execute(f"UPDATE {tabela} SET objetivos=? WHERE id=?", (sugerir_objetivos(*list(r)[1:]), r["id"]))
    c.execute("INSERT INTO config (chave, valor) VALUES ('ajuste_usabilidade_v1', '1')")
    c.commit()


def ajustes_v2():
    """Selo da home: "Compra segura via Hotmart" -> "Compra segura" (só se o texto ainda for o antigo)."""
    c = db()
    if c.execute("SELECT 1 FROM config WHERE chave='ajuste_v2'").fetchone():
        return
    r = c.execute("SELECT valor FROM config WHERE chave='hero_selos'").fetchone()
    if r and "Compra segura via Hotmart" in (r["valor"] or ""):
        c.execute("UPDATE config SET valor=? WHERE chave='hero_selos'", (r["valor"].replace("Compra segura via Hotmart", "Compra segura"),))
    c.execute("INSERT INTO config (chave, valor) VALUES ('ajuste_v2', '1')")
    c.commit()


with app.app_context():
    db().executescript(SCHEMA)
    db().executescript(acessos.SCHEMA)
    db().execute("PRAGMA journal_mode=WAL")  # leituras do painel não travam a gravação dos acessos
    migrate()
    seed()
    seed_once()
    migrate()  # dá endereço também aos materiais de exemplo criados agora
    ajustes_usabilidade_v1()
    ajustes_v2()


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
app.jinja_env.globals["OBJETIVOS"] = OBJETIVOS


@app.template_filter("objs")
def objs(v):
    """'produtividade,tecnico' -> lista de slugs válidos."""
    return [x for x in (v or "").split(",") if x in OBJ_POR_SLUG]


@app.template_filter("parcela")
def parcela_txt(p):
    """Texto das parcelas. A Hotmart pode cobrar juros no parcelamento, então não mostramos o valor da parcela."""
    n = p.get("parcelas") if hasattr(p, "get") else p["parcelas"]
    return f"ou em até {n}x no cartão" if n and n > 1 else ""


@app.template_filter("rotulo_formato")
def rotulo_formato(v):
    """'PDF · 24 páginas' -> 'PDF' (rótulo curto da miniatura)."""
    return re.split(r"\s*[·\-–|,]\s*", (v or "").strip())[0][:10] or "Grátis"



def agora_br():
    return datetime.now(FUSO)


def ler_data_oferta(texto):
    """'2026-11-28T23:59' (campo do painel) → datetime em horário de Brasília, ou None."""
    try:
        return datetime.strptime((texto or "").strip()[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=FUSO)
    except ValueError:
        return None


def com_oferta(row):
    """Produto pronto para o site. Com oferta no prazo, preço e link de compra viram os da oferta,
    e o preço normal aparece riscado. Fora do prazo, nada muda: o site volta sozinho ao normal."""
    p = dict(row)
    p["oferta"] = False
    fim = ler_data_oferta(p.get("oferta_fim"))
    if p.get("oferta_ativa") and p.get("oferta_preco") and fim and fim > agora_br():
        normal = p.get("preco") or p.get("preco_antigo")
        p.update(
            oferta=True,
            preco=p["oferta_preco"],
            preco_antigo=normal if normal and normal > p["oferta_preco"] else p.get("preco_antigo"),
            link=p.get("oferta_link") or p.get("link"),
            oferta_chamada=p.get("oferta_texto") or "Oferta por tempo limitado",
            oferta_fim_iso=fim.isoformat(),
            oferta_fim_txt=fim.strftime("%d/%m às %Hh%M"),
            oferta_agora_ms=int(agora_br().timestamp() * 1000),
        )
    return p


def produtos_no_ar():
    return [com_oferta(r) for r in db().execute("SELECT * FROM produtos WHERE ativo=1 ORDER BY ordem, id").fetchall()]


@app.context_processor
def inject():
    return {"S": get_settings() if request.endpoint not in ("static", "uploads") else {},
            "csrf_token": csrf_token, "ano": datetime.now().year, "pixel_id": META_PIXEL_ID}


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


GEO = acessos.Geo(DATA_DIR)
acessos.configurar(DATA_DIR)
PAGINAS_CONTADAS = {"home", "novidades", "materiais", "material_pagina", "loja", "produto_pagina", "post", "privacidade", "caminho"}


def registrar_acesso(tipo, alvo=""):
    try:
        ua = request.headers.get("User-Agent", "")
        if session.get("admin") or acessos.eh_robo(ua):
            return
        GEO.garantir()
        pagina = request.path
        if tipo == "hotmart" and request.referrer:
            pagina = urlparse(request.referrer).path or "/"
        acessos.registrar(db(), GEO, app.config["SECRET_KEY"], tipo=tipo, pagina=pagina, alvo=alvo,
                          ip=client_ip(), ua=ua, referrer=request.referrer or "", args=request.args,
                          host=request.host)
    except Exception as e:  # estatística nunca pode derrubar o site
        app.logger.warning("acesso não registrado: %s", e)


@app.after_request
def contar_acesso(resp):
    if (request.method == "GET" and resp.status_code == 200 and request.endpoint in PAGINAS_CONTADAS
            and "prefetch" not in (request.headers.get("Purpose", "") + request.headers.get("Sec-Purpose", ""))):
        registrar_acesso("pagina")
    return resp


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
    produtos = produtos_no_ar()
    prompts = [dict(texto=r["texto"], tag=r["tag"]) for r in
               c.execute("SELECT texto, tag FROM prompts WHERE ativo=1 ORDER BY ordem, id").fetchall()]
    categorias = []
    for p in posts:
        if p["categoria"] and p["categoria"] not in categorias:
            categorias.append(p["categoria"])
    return render_template("home.html", posts=posts, materiais=materiais, produtos=produtos,
                           prompts_json=json.dumps(prompts, ensure_ascii=False), categorias=categorias,
                           faq=parse_faq(get_settings().get("faq")))


def categorias_de(posts):
    cats = []
    for p in posts:
        if p["categoria"] and p["categoria"] not in cats:
            cats.append(p["categoria"])
    return cats


@app.route("/novidades")
def novidades():
    posts = db().execute("SELECT * FROM posts WHERE ativo=1 ORDER BY destaque DESC, ordem, criado_em DESC").fetchall()
    return render_template("novidades.html", posts=posts, categorias=categorias_de(posts))


@app.route("/materiais-gratis")
def materiais():
    itens = db().execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY ordem, id").fetchall()
    return render_template("materiais.html", materiais=itens)


@app.route("/materiais-gratis/<slug>")
def material_pagina(slug):
    c = db()
    m = c.execute("SELECT * FROM materiais WHERE slug=? AND (ativo=1 OR ?)",
                  (slug, 1 if session.get("admin") else 0)).fetchone()
    if not m:
        abort(404)
    outros = c.execute("SELECT * FROM materiais WHERE ativo=1 AND id<>? ORDER BY ordem, id LIMIT 3", (m["id"],)).fetchall()
    return render_template("material.html", m=m, outros=outros)


@app.route("/loja")
def loja():
    produtos = produtos_no_ar()
    para = request.args.get("para", "")
    if para not in OBJ_POR_SLUG:
        para = ""
    usados = [o for o in OBJETIVOS if any(o["slug"] in objs(p.get("objetivos")) for p in produtos)]
    if para:
        produtos = [p for p in produtos if para in objs(p.get("objetivos"))]
    return render_template("loja.html", produtos=produtos, faq=parse_faq(get_settings().get("faq")),
                           para=para, filtros=usados if len(usados) > 1 else [])


@app.route("/caminhos/<slug>")
def caminho(slug):
    obj = OBJ_POR_SLUG.get(slug)
    if not obj:
        abort(404)
    c = db()
    tem = lambda r: slug in objs(r["objetivos"])
    materiais = [r for r in c.execute("SELECT * FROM materiais WHERE ativo=1 ORDER BY ordem, id").fetchall() if tem(r)]
    posts = [r for r in c.execute("SELECT * FROM posts WHERE ativo=1 ORDER BY destaque DESC, ordem, criado_em DESC").fetchall() if tem(r)]
    produtos = [p for p in produtos_no_ar() if slug in objs(p.get("objetivos"))]
    return render_template("caminho.html", obj=obj, materiais=materiais, posts=posts, produtos=produtos)


@app.route("/robots.txt")
def robots():
    corpo = ("User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /material/\nDisallow: /api/\n\n"
             f"Sitemap: {url_for('sitemap', _external=True)}\n")
    return Response(corpo, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    c = db()
    urls = [url_for(e, _external=True) for e in ("home", "novidades", "materiais", "loja", "privacidade")]
    urls += [url_for("caminho", slug=o["slug"], _external=True) for o in OBJETIVOS]
    urls += [url_for("post", slug=r["slug"], _external=True) for r in c.execute("SELECT slug FROM posts WHERE ativo=1")]
    urls += [url_for("material_pagina", slug=r["slug"], _external=True)
             for r in c.execute("SELECT slug FROM materiais WHERE ativo=1 AND slug<>''")]
    urls += [url_for("produto_pagina", slug=r["pagina"], _external=True)
             for r in c.execute("SELECT pagina FROM produtos WHERE ativo=1 AND pagina<>''")]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"  <url><loc>{u}</loc></url>" for u in urls]
    xml.append("</urlset>")
    return Response("\n".join(xml), mimetype="application/xml")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(app.static_folder, "img"), "favicon.ico", max_age=60 * 60 * 24 * 30)


# Endereços alternativos que as pessoas costumam digitar
@app.route("/blog")
@app.route("/materiais")
@app.route("/gratis")
@app.route("/produtos")
def atalhos():
    destino = {"/blog": "novidades", "/materiais": "materiais", "/gratis": "materiais", "/produtos": "loja"}
    return redirect(url_for(destino[request.path]), code=301)


@app.route("/blog/<slug>")
def post(slug):
    p = db().execute("SELECT * FROM posts WHERE slug=? AND (ativo=1 OR ?)", (slug, 1 if session.get("admin") else 0)).fetchone()
    if not p:
        abort(404)
    outros = db().execute("SELECT * FROM posts WHERE ativo=1 AND id<>? ORDER BY criado_em DESC LIMIT 3", (p["id"],)).fetchall()
    return render_template("post.html", p=p, outros=outros)


@app.route("/produtos/<slug>")
def produto_pagina(slug):
    p = db().execute("SELECT * FROM produtos WHERE pagina=? AND (ativo=1 OR ?)",
                     (slug, 1 if session.get("admin") else 0)).fetchone()
    if not p:
        abort(404)
    especifico = f"produtos/{secure_filename(slug)}.html"
    tpl = especifico if os.path.exists(os.path.join(BASE_DIR, "templates", especifico)) else "produtos/_padrao.html"
    return render_template(tpl, p=com_oferta(p))


@app.route("/privacidade")
def privacidade():
    return render_template("privacidade.html")


@app.route("/material/<int:mid>")
def material(mid):
    m = db().execute("SELECT * FROM materiais WHERE id=? AND ativo=1", (mid,)).fetchone()
    if not m:
        abort(404)
    if m["arquivo"] or m["link"]:
        registrar_acesso("download", m["titulo"])
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


@app.route("/api/clique", methods=["POST"])
def api_clique():
    data = request.get_json(silent=True, force=True) or {}
    if not too_many("clique:" + client_ip(), 30, 600):
        alvo = str(data.get("produto") or data.get("url") or "")[:200]
        registrar_acesso("hotmart", alvo)
    return ("", 204)


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
    ofertas = [p for p in produtos_no_ar() if p["oferta"]]
    return render_template("admin/home.html", contagem=contagem, ativos=ativos, inscritos=inscritos,
                           semana=semana, recentes=recentes, sem_link=sem_link, ofertas=ofertas, entities=ENTITIES)


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
            elif ftype == "objetivos":
                values[name] = ",".join(x for x in form.getlist(name) if x in OBJ_POR_SLUG)
            elif ftype == "datetime":
                bruto = (form.get(name) or "").strip()
                if bruto and not ler_data_oferta(bruto):
                    erros.append(f"{label}: data inválida.")
                values[name] = bruto[:16]
            else:
                values[name] = (form.get(name) or "").strip()
            if opts.get("required") and not values.get(name):
                erros.append("Classificação: marque pelo menos um caminho." if ftype == "objetivos"
                             else f"Preencha o campo “{label}”.")
        values["ativo"] = 1 if form.get("ativo") else 0
        try:
            values["ordem"] = int(form.get("ordem") or 0)
        except ValueError:
            values["ordem"] = 0

        if kind == "produtos" and values.get("pagina"):
            values["pagina"] = slugify(values["pagina"])
            if c.execute("SELECT id FROM produtos WHERE pagina=? AND id<>?", (values["pagina"], item_id or 0)).fetchone():
                erros.append("Já existe outro produto usando esse endereço de página.")

        if kind == "produtos" and values.get("oferta_ativa"):
            fim = ler_data_oferta(values.get("oferta_fim"))
            if not values.get("oferta_preco"):
                erros.append("Oferta: preencha o preço da oferta.")
            elif values.get("preco") and values["oferta_preco"] >= values["preco"]:
                erros.append("Oferta: o preço da oferta precisa ser menor que o preço normal.")
            if not fim:
                erros.append("Oferta: preencha quando a oferta termina.")
            elif fim <= agora_br():
                flash("Atenção: a data de fim da oferta já passou, então o contador não vai aparecer no site.", "erro")
            if not values.get("oferta_link") and not values.get("link"):
                erros.append("Oferta: cole o link do checkout com o cupom (ou o link normal da Hotmart).")

        if kind == "materiais":
            usados = {r[0] for r in c.execute("SELECT slug FROM materiais WHERE id<>? AND slug<>''", (item_id or 0,))}
            values["slug"] = slug_livre(values.get("slug") or values.get("titulo") or "", usados)

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


@app.route("/admin/acessos")
@login_required
def admin_acessos():
    c = db()
    GEO.garantir()
    acessos._arquivar_seguro(c)
    nomes = {"/": "Início", "/novidades": "Novidades", "/materiais-gratis": "Materiais grátis",
             "/loja": "Loja", "/privacidade": "Política de privacidade"}
    for r in c.execute("SELECT slug, titulo FROM posts"):
        nomes[f"/blog/{r['slug']}"] = r["titulo"]
    for r in c.execute("SELECT slug, titulo FROM materiais WHERE slug<>''"):
        nomes[f"/materiais-gratis/{r['slug']}"] = r["titulo"]
    for r in c.execute("SELECT pagina, titulo FROM produtos WHERE pagina<>''"):
        nomes[f"/produtos/{r['pagina']}"] = r["titulo"]

    mes = request.args.get("mes", "")
    if re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", mes):
        rel, dias = acessos.relatorio_mes(c, mes, nomes), 0
    else:
        try:
            dias = int(request.args.get("dias", 30))
        except ValueError:
            dias = 30
        dias, mes = (dias if dias in (1, 7, 30) else 30), ""
        rel = acessos.relatorio_periodo(c, dias, nomes)

    links = [("Início", url_for("home", _external=True)), ("Materiais grátis", url_for("materiais", _external=True)),
             ("Loja", url_for("loja", _external=True)), ("Novidades", url_for("novidades", _external=True))]
    links += [(f"Material: {r['titulo']}", url_for("material_pagina", slug=r["slug"], _external=True))
              for r in c.execute("SELECT slug, titulo FROM materiais WHERE ativo=1 AND slug<>'' ORDER BY ordem, id")]
    links += [(f"Produto: {r['titulo']}", url_for("produto_pagina", slug=r["pagina"], _external=True))
              for r in c.execute("SELECT pagina, titulo FROM produtos WHERE ativo=1 AND pagina<>'' ORDER BY ordem, id")]
    banco, arquivos = acessos.espaco_usado(DB_PATH)
    return render_template("admin/acessos.html", r=rel, dias=dias, mes=mes, nomes=nomes, links=links,
                           historico=acessos.historico(c), espaco={"banco": banco, "arquivos": arquivos},
                           mantido_desde=acessos.primeiro_dia_mantido(), geo_status=GEO.status(), entities=ENTITIES)


@app.route("/admin/acessos/<mes>.zip")
@login_required
def admin_acessos_zip(mes):
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", mes):
        abort(404)
    arq = acessos.arquivo_do_mes(mes)
    if os.path.exists(arq):
        return send_from_directory(os.path.dirname(arq), os.path.basename(arq), as_attachment=True)
    # Mês ainda com detalhe guardado: gera na hora
    texto = acessos.csv_do_mes_atual(db(), mes)
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"acessos-{mes}.csv", texto)
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=acessos-{mes}.zip"})


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
