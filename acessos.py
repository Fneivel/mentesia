"""Acompanhamento de acessos do Mentes.IA (sem serviços de terceiros).

O que é guardado por acesso: data/hora, página, origem (Instagram, Facebook, TikTok…),
campanha, tipo de aparelho, navegador e localização aproximada (cidade/estado/país).
O IP NÃO é guardado: ele só é usado no momento do acesso para achar a cidade e para
gerar um código anônimo do visitante, que muda todo dia (conta visitantes únicos sem
identificar ninguém).

A localização vem da base gratuita DB-IP City Lite (CC BY 4.0), baixada uma vez por mês
para a pasta de dados. Se a base ainda não estiver disponível, os acessos são
registrados normalmente, só que sem cidade.
"""
import gzip
import hashlib
import os
import re
import shutil
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    FUSO = None

try:
    import maxminddb
except ImportError:  # pragma: no cover
    maxminddb = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS acessos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quando TEXT NOT NULL, dia TEXT NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'pagina',
  pagina TEXT DEFAULT '', alvo TEXT DEFAULT '',
  origem TEXT DEFAULT '', ref TEXT DEFAULT '', campanha TEXT DEFAULT '',
  visitante TEXT DEFAULT '', dispositivo TEXT DEFAULT '', navegador TEXT DEFAULT '',
  pais TEXT DEFAULT '', estado TEXT DEFAULT '', cidade TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_acessos_dia ON acessos(dia);
CREATE INDEX IF NOT EXISTS ix_acessos_vis ON acessos(visitante, dia);
CREATE TABLE IF NOT EXISTS acessos_mensal (
  mes TEXT NOT NULL, dimensao TEXT NOT NULL, nome TEXT NOT NULL,
  n INTEGER DEFAULT 0, extra INTEGER DEFAULT 0,
  PRIMARY KEY (mes, dimensao, nome)
);
"""

# Detalhe de cada acesso: só o mês atual e o anterior. Meses mais antigos viram um resumo
# (tabela acessos_mensal) + um CSV compactado na pasta de dados, e o detalhe é apagado.
COLUNAS_CSV = ["quando", "tipo", "pagina", "alvo", "origem", "ref", "campanha", "visitante",
               "dispositivo", "navegador", "pais", "estado", "cidade"]

DIRETO = "Direto / sem origem"
REDES = ["Instagram", "Facebook", "TikTok", "WhatsApp", "YouTube", "LinkedIn", "X (Twitter)", "Threads",
         "Pinterest", "Google", "Bing", "E-mail", "Hotmart"]


def agora():
    return datetime.now(FUSO) if FUSO else datetime.now()


# ---------------------------------------------------------------------
# Robôs e prévias de link não contam como acesso
# ---------------------------------------------------------------------
ROBO = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|facebookcatalog|meta-externalagent|whatsapp/|"
    r"telegram|discord|skype|embedly|quora link|vkshare|pinterest/|bitlybot|headless|lighthouse|"
    r"python-requests|curl/|wget|go-http|java/|okhttp|httpclient|axios|node-fetch|monitor|uptime|render",
    re.I)


def eh_robo(ua):
    return not ua or bool(ROBO.search(ua))


# ---------------------------------------------------------------------
# De onde a pessoa veio
# ---------------------------------------------------------------------
UTM = {
    "instagram": "Instagram", "ig": "Instagram", "insta": "Instagram",
    "facebook": "Facebook", "fb": "Facebook", "face": "Facebook",
    "tiktok": "TikTok", "tt": "TikTok",
    "whatsapp": "WhatsApp", "wa": "WhatsApp", "zap": "WhatsApp", "wpp": "WhatsApp",
    "youtube": "YouTube", "yt": "YouTube",
    "linkedin": "LinkedIn", "x": "X (Twitter)", "twitter": "X (Twitter)", "threads": "Threads",
    "pinterest": "Pinterest", "google": "Google", "email": "E-mail", "e-mail": "E-mail",
    "newsletter": "E-mail", "hotmart": "Hotmart",
}

# Navegadores embutidos dos apps se identificam no "User-Agent", mesmo sem enviar a página de origem
APP_UA = [
    (re.compile(r"Instagram", re.I), "Instagram"),
    (re.compile(r"FBAN|FBAV|FB_IAB|FBIOS", re.I), "Facebook"),
    (re.compile(r"musical_ly|Bytedance|TikTok|trill_", re.I), "TikTok"),
    (re.compile(r"Barcelona", re.I), "Threads"),
    (re.compile(r"LinkedInApp", re.I), "LinkedIn"),
    (re.compile(r"Pinterest", re.I), "Pinterest"),
]

REF = [
    (r"(^|\.)instagram\.com$", "Instagram"),
    (r"(^|\.)(facebook\.com|fb\.com|fb\.me|messenger\.com)$", "Facebook"),
    (r"(^|\.)tiktok\.com$", "TikTok"),
    (r"(^|\.)(whatsapp\.com|wa\.me)$", "WhatsApp"),
    (r"(^|\.)(youtube\.com|youtu\.be)$", "YouTube"),
    (r"(^|\.)(linkedin\.com|lnkd\.in)$", "LinkedIn"),
    (r"(^|\.)(t\.co|x\.com|twitter\.com)$", "X (Twitter)"),
    (r"(^|\.)threads\.(net|com)$", "Threads"),
    (r"(^|\.)pinterest\.[a-z.]+$", "Pinterest"),
    (r"(^|\.)(mail\.google\.com|outlook\.[a-z.]+|mail\.yahoo\.com)$", "E-mail"),
    (r"(^|\.)google\.[a-z.]+$", "Google"),
    (r"(^|\.)bing\.com$", "Bing"),
    (r"(^|\.)(duckduckgo\.com|search\.yahoo\.com|yandex\.[a-z.]+|ecosia\.org)$", "Outras buscas"),
    (r"(^|\.)hotmart\.com$", "Hotmart"),
    (r"(^|\.)(chatgpt\.com|chat\.openai\.com|perplexity\.ai|claude\.ai|gemini\.google\.com)$", "Assistentes de IA"),
]
REF = [(re.compile(p, re.I), n) for p, n in REF]


def classificar(args, ua, referrer, host_proprio):
    """Devolve (origem, dominio_de_origem, campanha, interno)."""
    campanha = (args.get("utm_campaign") or "").strip()[:80]
    ref_host = ""
    if referrer:
        try:
            ref_host = (urlparse(referrer).hostname or "").lower().removeprefix("www.")
        except ValueError:
            ref_host = ""
    proprio = (host_proprio or "").lower().split(":")[0].removeprefix("www.")
    interno = bool(ref_host) and ref_host == proprio

    fonte = (args.get("utm_source") or "").strip().lower()
    if fonte:
        return UTM.get(fonte, fonte[:40].capitalize()), ref_host, campanha, False
    for rx, nome in APP_UA:
        if ua and rx.search(ua):
            return nome, ref_host, campanha, False
    if interno:
        return "", ref_host, campanha, True
    if ref_host:
        for rx, nome in REF:
            if rx.search(ref_host):
                return nome, ref_host, campanha, False
        return ref_host[:60], ref_host, campanha, False
    if args.get("fbclid"):
        return "Facebook / Instagram", "", campanha, False
    if args.get("ttclid"):
        return "TikTok", "", campanha, False
    if args.get("gclid"):
        return "Google", "", campanha, False
    return DIRETO, "", campanha, False


def aparelho(ua):
    ua = ua or ""
    if re.search(r"iPad|Tablet|Android(?!.*Mobile)", ua):
        disp = "Tablet"
    elif re.search(r"Mobi|iPhone|Android", ua):
        disp = "Celular"
    else:
        disp = "Computador"
    if re.search(r"Instagram|FBAN|FBAV|musical_ly|Bytedance|Barcelona|LinkedInApp", ua):
        nav = "Navegador do app"
    elif "Edg/" in ua:
        nav = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        nav = "Opera"
    elif "SamsungBrowser" in ua:
        nav = "Samsung Internet"
    elif "Firefox/" in ua:
        nav = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        nav = "Chrome"
    elif "Safari/" in ua:
        nav = "Safari"
    else:
        nav = "Outro"
    return disp, nav


# ---------------------------------------------------------------------
# Localização aproximada pelo IP (base local DB-IP City Lite)
# ---------------------------------------------------------------------
class Geo:
    def __init__(self, pasta):
        self.arquivo = os.path.join(pasta, "geo", "dbip-city-lite.mmdb")
        self.leitor = None
        self.carregado_em = 0
        self.baixando = False
        self.erro = ""
        self.lock = threading.Lock()

    def status(self):
        if self.leitor:
            mes = datetime.fromtimestamp(os.path.getmtime(self.arquivo)).strftime("%m/%Y")
            return f"Base de localização carregada (atualizada em {mes})."
        if self.baixando:
            return "Baixando a base de localização (leva alguns minutos na primeira vez)."
        if not maxminddb:
            return "Pacote maxminddb não instalado: localização desligada."
        return "Base de localização ainda não disponível." + (f" Último erro: {self.erro}" if self.erro else "")

    def _abrir(self):
        if maxminddb and os.path.exists(self.arquivo):
            try:
                novo = maxminddb.open_database(self.arquivo)
                antigo, self.leitor = self.leitor, novo
                self.carregado_em = os.path.getmtime(self.arquivo)
                if antigo:
                    antigo.close()
            except Exception as e:  # arquivo corrompido: apaga para baixar de novo
                self.erro = str(e)[:120]
                try:
                    os.remove(self.arquivo)
                except OSError:
                    pass

    def garantir(self):
        """Abre a base se existir; baixa em segundo plano se faltar ou tiver mais de 35 dias."""
        if not maxminddb:
            return
        if not self.leitor:
            self._abrir()
        velho = not os.path.exists(self.arquivo) or time.time() - os.path.getmtime(self.arquivo) > 35 * 86400
        if velho and not self.baixando and time.time() - getattr(self, "_tentou", 0) > 3600:
            self._tentou = time.time()
            self.baixando = True
            threading.Thread(target=self._baixar, daemon=True).start()

    def _baixar(self):
        try:
            os.makedirs(os.path.dirname(self.arquivo), exist_ok=True)
            hoje = datetime.now()
            meses = [hoje.strftime("%Y-%m"), (hoje.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")]
            for mes in meses:
                url = f"https://download.db-ip.com/free/dbip-city-lite-{mes}.mmdb.gz"
                tmp = self.arquivo + ".baixando"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mentes.IA/1.0"})
                    with urllib.request.urlopen(req, timeout=120) as r, open(tmp + ".gz", "wb") as f:
                        shutil.copyfileobj(r, f)
                    with gzip.open(tmp + ".gz", "rb") as g, open(tmp, "wb") as f:
                        shutil.copyfileobj(g, f)
                    os.replace(tmp, self.arquivo)
                    self.erro = ""
                    break
                except Exception as e:
                    self.erro = f"{mes}: {str(e)[:100]}"
                finally:
                    for p in (tmp, tmp + ".gz"):
                        if os.path.exists(p):
                            os.remove(p)
            with self.lock:
                self._abrir()
        finally:
            self.baixando = False

    def buscar(self, ip):
        if not self.leitor or not ip:
            return "", "", ""
        try:
            d = self.leitor.get(ip) or {}
        except Exception:
            return "", "", ""

        def nome(obj):
            n = (obj or {}).get("names") or {}
            return n.get("pt-BR") or n.get("en") or ""
        subs = d.get("subdivisions") or [{}]
        return nome(d.get("country"))[:60], nome(subs[0])[:60], nome(d.get("city"))[:60]


# ---------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------
_ultima_limpeza = {"dia": ""}


def visitante_do_dia(segredo, ip, ua, dia):
    return hashlib.sha256(f"{segredo}|{ip}|{ua}|{dia}".encode()).hexdigest()[:16]


def registrar(conn, geo, segredo, *, tipo, pagina, ip, ua, referrer, args, host, alvo=""):
    t = agora()
    dia = t.strftime("%Y-%m-%d")
    vis = visitante_do_dia(segredo, ip, ua, dia)
    origem, ref, campanha, interno = classificar(args, ua, referrer, host)
    if interno:
        # Navegação dentro do site (ou clique num botão): herda a origem com que a pessoa chegou hoje
        ant = conn.execute("SELECT origem, campanha FROM acessos WHERE visitante=? AND dia=? AND origem<>'' "
                           "ORDER BY id LIMIT 1", (vis, dia)).fetchone()
        origem, campanha = (ant[0], campanha or ant[1]) if ant else (DIRETO, campanha)
    disp, nav = aparelho(ua)
    pais, estado, cidade = geo.buscar(ip)
    conn.execute("INSERT INTO acessos (quando, dia, tipo, pagina, alvo, origem, ref, campanha, visitante, dispositivo,"
                 " navegador, pais, estado, cidade) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (t.strftime("%Y-%m-%d %H:%M:%S"), dia, tipo, pagina[:200], alvo[:200], origem, ref, campanha,
                  vis, disp, nav, pais, estado, cidade))
    conn.commit()
    if _ultima_limpeza["dia"] != dia:
        _ultima_limpeza["dia"] = dia
        _arquivar_seguro(conn)


# ---------------------------------------------------------------------
# Arquivamento mensal: resumo + CSV, depois apaga o detalhe
# ---------------------------------------------------------------------
PASTA_ARQUIVO = {"caminho": ""}


def configurar(pasta_dados):
    PASTA_ARQUIVO["caminho"] = os.path.join(pasta_dados, "acessos-arquivo")
    os.makedirs(PASTA_ARQUIVO["caminho"], exist_ok=True)


def arquivo_do_mes(mes):
    return os.path.join(PASTA_ARQUIVO["caminho"], f"acessos-{mes}.zip")


def primeiro_dia_mantido(hoje=None):
    """Primeiro dia do mês anterior: dali para frente o detalhe continua guardado."""
    hoje = hoje or agora().date()
    return (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)


def _agregar(conn, filtro, params):
    """Totais e rankings de um intervalo de dados detalhados. Devolve [(dimensao, nome, n, extra)]."""
    w = f"WHERE {filtro}"
    linhas = []
    t = conn.execute(f"SELECT SUM(tipo='pagina'), COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante||dia END),"
                     f" SUM(tipo='download'), SUM(tipo='hotmart') FROM acessos {w}", params).fetchone()
    for nome, v in zip(("visitas", "visitantes", "downloads", "hotmart"), t):
        linhas.append(("total", nome, v or 0, 0))
    consultas = {
        "dia": f"SELECT dia, COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante END), SUM(tipo='pagina') FROM acessos {w} GROUP BY dia",
        "origem": f"SELECT origem, COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante||dia END), SUM(tipo='download') FROM acessos {w} GROUP BY origem",
        "pagina": f"SELECT pagina, COUNT(*), COUNT(DISTINCT visitante||dia) FROM acessos {w} AND tipo='pagina' GROUP BY pagina",
        "cidade": f"SELECT CASE WHEN cidade<>'' THEN cidade||' · '||estado ELSE '' END, COUNT(DISTINCT visitante||dia), 0 FROM acessos {w} AND tipo='pagina' GROUP BY 1",
        "estado": f"SELECT CASE WHEN estado<>'' THEN estado||' · '||pais ELSE '' END, COUNT(DISTINCT visitante||dia), 0 FROM acessos {w} AND tipo='pagina' GROUP BY 1",
        "aparelho": f"SELECT dispositivo, COUNT(DISTINCT visitante||dia), 0 FROM acessos {w} AND tipo='pagina' GROUP BY 1",
        "campanha": f"SELECT campanha||' · '||origem, COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante||dia END), SUM(tipo='download') FROM acessos {w} AND campanha<>'' GROUP BY campanha, origem",
        "download": f"SELECT alvo, COUNT(*), 0 FROM acessos {w} AND tipo='download' GROUP BY alvo",
        "hotmart": f"SELECT alvo, COUNT(*), 0 FROM acessos {w} AND tipo='hotmart' GROUP BY alvo",
    }
    for dim, sql in consultas.items():
        for nome, n, extra in conn.execute(sql, params):
            if (n or 0) or (extra or 0):
                linhas.append((dim, nome or "", n or 0, extra or 0))
    return linhas


def _escrever_zip(conn, mes, destino):
    import csv
    import io
    import zipfile
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(COLUNAS_CSV)
    for r in conn.execute(f"SELECT {', '.join(COLUNAS_CSV)} FROM acessos WHERE substr(dia,1,7)=? ORDER BY id", (mes,)):
        wr.writerow(list(r))
    tmp = destino + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(f"acessos-{mes}.csv", "﻿" + buf.getvalue())
    os.replace(tmp, destino)


def csv_do_mes_atual(conn, mes):
    """CSV (texto) de um mês que ainda tem detalhe guardado."""
    import csv
    import io
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(COLUNAS_CSV)
    for r in conn.execute(f"SELECT {', '.join(COLUNAS_CSV)} FROM acessos WHERE substr(dia,1,7)=? ORDER BY id", (mes,)):
        wr.writerow(list(r))
    return "﻿" + buf.getvalue()


def arquivar(conn, hoje=None):
    """Fecha os meses antigos. Devolve a lista de meses arquivados."""
    limite = primeiro_dia_mantido(hoje).isoformat()
    meses = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(dia,1,7) FROM acessos WHERE dia < ? ORDER BY 1", (limite,))]
    for mes in meses:
        destino = arquivo_do_mes(mes)
        if os.path.exists(destino):  # sobrou detalhe de um mês já arquivado: guarda num arquivo extra
            destino = destino.replace(".zip", f"-{int(time.time())}.zip")
        _escrever_zip(conn, mes, destino)
        for dim, nome, n, extra in _agregar(conn, "substr(dia,1,7)=?", (mes,)):
            conn.execute("INSERT INTO acessos_mensal (mes, dimensao, nome, n, extra) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(mes, dimensao, nome) DO UPDATE SET n=n+excluded.n, extra=extra+excluded.extra",
                         (mes, dim, nome, n, extra))
        conn.execute("DELETE FROM acessos WHERE substr(dia,1,7)=?", (mes,))
        conn.commit()
    if meses:
        conn.execute("VACUUM")  # devolve ao disco o espaço do detalhe apagado
    return meses


def _arquivar_seguro(conn):
    if not PASTA_ARQUIVO["caminho"]:
        return
    try:
        arquivar(conn)
    except Exception:  # nunca atrapalha o registro do acesso
        conn.rollback()


# ---------------------------------------------------------------------
# Relatório para o painel
# ---------------------------------------------------------------------
def _montar(linhas, inicio, fim, nomes_pagina, limites=None):
    """Transforma [(dimensao, nome, n, extra)] no formato que a tela usa."""
    dims = {}
    for dim, nome, n, extra in linhas:
        dims.setdefault(dim, []).append((nome, n, extra))
    tot = {nome: n for nome, n, _ in dims.get("total", [])}
    resumo = {"visitas": tot.get("visitas", 0), "visitantes": tot.get("visitantes", 0),
              "downloads": tot.get("downloads", 0), "hotmart": tot.get("hotmart", 0), "visitantes_antes": 0}

    por_dia = {nome: (n, extra) for nome, n, extra in dims.get("dia", [])}
    serie, d = [], inicio
    while d <= fim:
        u, v = por_dia.get(d.isoformat(), (0, 0))
        serie.append({"dia": d, "visitantes": u, "visitas": v})
        d += timedelta(days=1)

    def ranking(dim, limite=10):
        itens = sorted(dims.get(dim, []), key=lambda x: (-x[1], x[0]))[:limite]
        maior = max([x[1] for x in itens] or [1]) or 1
        return [{"nome": nome or "Não identificado", "n": n, "extra": extra, "pct": round(100 * n / maior),
                 "titulo": nomes_pagina.get(nome) if dim == "pagina" else None} for nome, n, extra in itens]

    return {"resumo": resumo, "serie": serie, "origens": ranking("origem", 12), "paginas": ranking("pagina", 12),
            "cidades": ranking("cidade", 12), "estados": ranking("estado"), "aparelhos": ranking("aparelho", 5),
            "campanhas": ranking("campanha"), "downloads": ranking("download"), "cliques": ranking("hotmart"),
            "ultimos": [], "inicio": inicio, "fim": fim}


def relatorio_periodo(conn, dias, nomes_pagina):
    """Últimos N dias, a partir do detalhe guardado."""
    hoje = agora().date()
    inicio = hoje - timedelta(days=dias - 1)
    rel = _montar(_agregar(conn, "dia>=?", (inicio.isoformat(),)), inicio, hoje, nomes_pagina)
    ant_ini = (inicio - timedelta(days=dias)).isoformat()
    rel["resumo"]["visitantes_antes"] = conn.execute(
        "SELECT COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante||dia END) FROM acessos WHERE dia>=? AND dia<?",
        (ant_ini, inicio.isoformat())).fetchone()[0] or 0
    rel["ultimos"] = conn.execute("SELECT * FROM acessos ORDER BY id DESC LIMIT 20").fetchall()
    return rel


def relatorio_mes(conn, mes, nomes_pagina):
    """Um mês inteiro: do detalhe se ainda existir, senão do resumo arquivado."""
    inicio = datetime.strptime(mes + "-01", "%Y-%m-%d").date()
    fim = (inicio.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    fim = min(fim, agora().date()) if inicio <= agora().date() else fim
    linhas = [tuple(r) for r in conn.execute(
        "SELECT dimensao, nome, n, extra FROM acessos_mensal WHERE mes=?", (mes,))]
    detalhe = conn.execute("SELECT 1 FROM acessos WHERE substr(dia,1,7)=? LIMIT 1", (mes,)).fetchone()
    if detalhe:  # mês ainda aberto (ou com sobra): soma o detalhe ao que já estiver resumido
        extra = _agregar(conn, "substr(dia,1,7)=?", (mes,))
        soma = {}
        for dim, nome, n, ex in linhas + extra:
            a = soma.setdefault((dim, nome), [0, 0])
            a[0] += n
            a[1] += ex
        linhas = [(d, nm, v[0], v[1]) for (d, nm), v in soma.items()]
    rel = _montar(linhas, inicio, fim, nomes_pagina)
    rel["arquivado"] = not detalhe
    return rel


def historico(conn):
    """Lista de meses com totais, do mais recente para o mais antigo."""
    meses = {}
    for mes, nome, n in conn.execute("SELECT mes, nome, n FROM acessos_mensal WHERE dimensao='total'"):
        meses.setdefault(mes, {"visitantes": 0, "visitas": 0, "downloads": 0, "hotmart": 0})[nome] = n
    for mes, vis, vt, dl, hm in conn.execute(
            "SELECT substr(dia,1,7), COUNT(DISTINCT CASE WHEN tipo='pagina' THEN visitante||dia END), SUM(tipo='pagina'),"
            " SUM(tipo='download'), SUM(tipo='hotmart') FROM acessos GROUP BY 1"):
        m = meses.setdefault(mes, {"visitantes": 0, "visitas": 0, "downloads": 0, "hotmart": 0})
        m["visitantes"] += vis or 0
        m["visitas"] += vt or 0
        m["downloads"] += dl or 0
        m["hotmart"] += hm or 0
    saida = []
    for mes in sorted(meses, reverse=True):
        arq = arquivo_do_mes(mes) if PASTA_ARQUIVO["caminho"] else ""
        saida.append({"mes": mes, **meses[mes], "arquivado": bool(arq) and os.path.exists(arq),
                      "tamanho": os.path.getsize(arq) if arq and os.path.exists(arq) else 0})
    return saida


def espaco_usado(db_path):
    total = 0
    for p in (db_path, db_path + "-wal"):
        if os.path.exists(p):
            total += os.path.getsize(p)
    pasta = PASTA_ARQUIVO["caminho"]
    arquivos = sum(os.path.getsize(os.path.join(pasta, f)) for f in os.listdir(pasta)) if pasta and os.path.isdir(pasta) else 0
    return total, arquivos
