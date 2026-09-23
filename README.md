# MentesIA — site + painel administrativo

Site www.mentesia.com.br com painel em **/admin** para editar tudo sem mexer em código:

| No painel | O que muda no site |
|---|---|
| **Artigos** | Blog da home e página de cada artigo (`/blog/endereco`). Texto com formatação simples, capa, categoria, rascunho, destaque. |
| **Materiais grátis** | Cards de materiais. O botão leva a um link (ex.: produto gratuito na Hotmart) ou baixa um arquivo enviado (PDF, planilha…). |
| **Produtos (Hotmart)** | Vitrine da loja: nome, preço, preço riscado, parcelas, selo, imagem e **link de checkout da Hotmart**. |
| **Prompts do dia** | Os prompts que giram no quadro do topo. |
| **Textos do site** | Títulos, chamadas, botões, perguntas frequentes, rodapé, redes sociais e descrição para o Google. |
| **Inscritos** | E-mails da newsletter, com exportação para CSV (Excel, Brevo, Mailchimp, RD Station). |
| **Arquivos e imagens** | Envio de imagens e arquivos, com endereço para copiar. |

Em todas as listas você pode publicar/ocultar com um clique e **arrastar para mudar a ordem**.

---

## 1. Rodar no seu computador (Windows)

Pré-requisito: Python 3.10 ou mais novo ([python.org](https://www.python.org/downloads/), marque "Add Python to PATH").

```powershell
cd caminho\para\mentesia-app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env          # troque ADMIN_PASSWORD e SECRET_KEY
python app.py
```

Abra http://127.0.0.1:5000 (site) e http://127.0.0.1:5000/admin (painel).

Na primeira execução o banco é criado em `data/mentesia.db` com o conteúdo de exemplo. Os arquivos enviados ficam em `data/uploads/`.
**Faça backup da pasta `data/`**: é ali que está todo o seu conteúdo.

---

## 2. Publicar no Render

1. Suba esta pasta para um repositório no GitHub (o `.gitignore` já impede que `.env` e `data/` subam).
2. No Render: **New → Blueprint** e escolha o repositório. O `render.yaml` já configura tudo.
3. Em **Environment**, defina `ADMIN_PASSWORD` (a senha do painel).
4. Depois do primeiro deploy, em **Settings → Custom Domains**, adicione `www.mentesia.com.br` e `mentesia.com.br` e crie no Registro.br os registros DNS que o Render mostrar.

> **Importante: disco persistente.** O banco e os uploads ficam em `/var/data`, um disco que exige plano pago no Render (Starter). No plano gratuito o disco é apagado a cada deploy ou reinício, e você perderia o que editou no painel.
> Alternativa: qualquer VPS (Hostinger, Contabo, DigitalOcean) rodando `gunicorn app:app --workers 1 --threads 4`, com `DATA_DIR` apontando para uma pasta com backup.

---

## 3. Configurações (`.env`)

| Variável | Para que serve |
|---|---|
| `ADMIN_USER` / `ADMIN_PASSWORD` | Login do painel. Use uma senha forte. |
| `SECRET_KEY` | Assina o cookie de login. Gere com `python -c "import secrets; print(secrets.token_hex(32))"`. Se mudar, todos são deslogados. |
| `DATA_DIR` | Pasta do banco e dos uploads. |
| `PRODUCTION` | `1` em produção (HTTPS). |
| `MAX_UPLOAD_MB` | Tamanho máximo de arquivo enviado (padrão 25). |

---

## 4. Formatação do texto dos artigos

```
## Subtítulo
Texto com **negrito**, *itálico* e [link](https://exemplo.com).

- item de lista
- outro item

![descrição da imagem](endereço copiado em "Arquivos e imagens")
```

O botão **Pré-visualizar** no editor mostra como vai ficar.

---

## 5. Segurança já incluída

- Senha guardada em hash; bloqueio após 5 tentativas erradas em 10 minutos.
- Proteção CSRF em todos os formulários do painel.
- Upload só de extensões permitidas; SVG servido isolado.
- Newsletter com proteção contra robôs e limite por IP.
- Painel marcado como `noindex` (não aparece no Google).

## Estrutura

```
app.py                 aplicação (rotas do site, painel, banco)
templates/             páginas do site (base, home, post, 404)
templates/admin/       telas do painel
static/css, static/js  estilos e scripts
static/img             logo e ícone
data/                  banco SQLite e uploads (criado automaticamente)
```
