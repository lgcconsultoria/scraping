# Plataforma LGC — Manual do Usuário

> **Versão:** branch `claude/eager-edison-e4htey`  
> **Contato:** lgclicitacao@gmail.com  
> **Requisito mínimo:** Python 3.10+

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Instalação](#2-instalação)
3. [Arquitetura em camadas](#3-arquitetura-em-camadas)
4. [Módulo `nucleo/` — Infraestrutura compartilhada](#4-módulo-nucleo)
5. [Módulo `leis/` — Repositório local de textos legais](#5-módulo-leis)
6. [Módulo `tribunais/` — Adaptadores de portais judiciais](#6-módulo-tribunais)
7. [Módulo `acervo/` — Banco de dados de decisões](#7-módulo-acervo)
8. [Módulo `qa/` — Gate anti-alucinação e rubrica](#8-módulo-qa)
9. [Módulo `agentes/` — Pesquisador e redator](#9-módulo-agentes)
10. [Módulo `orquestrador/` — Fluxo completo](#10-módulo-orquestrador)
11. [Scripts de linha de comando](#11-scripts-de-linha-de-comando)
12. [Fluxo de trabalho típico](#12-fluxo-de-trabalho-típico)
13. [Variáveis de ambiente](#13-variáveis-de-ambiente)
14. [Segurança e boas práticas](#14-segurança-e-boas-práticas)
15. [Solução de problemas](#15-solução-de-problemas)

---

## 1. Visão geral

A plataforma LGC é um sistema Python de pesquisa jurídica automatizada que:

1. **Coleta** acórdãos de tribunais estaduais (TJSC, TJSP, TJMS) via scraping respeitoso
2. **Armazena** as decisões em um banco SQLite com busca de texto integral (FTS5)
3. **Mantém** cópias locais versionadas dos textos do CPC, CC e Lei de Alimentos
4. **Valida** citações legais com um gate anti-alucinação de três camadas
5. **Gera** rascunhos de peças jurídicas (tutelas de urgência, petições de alimentos)
6. **Orquestra** o fluxo completo com gates de aprovação humana obrigatórios

```
Portal TJSC ─┐
Portal TJSP ─┤→ tribunais/ → acervo/db → qa/gate → agentes/redator → orquestrador/
Portal TJMS ─┘              ↑
Planalto.gov ───→ leis/ ────┘
```

---

## 2. Instalação

```bash
# 1. Clone o repositório
git clone <repo_url>
cd scraping

# 2. Crie e ative um ambiente virtual
python -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows

# 3. Instale as dependências
pip install -r requirements.txt

# Dependências mínimas (coleta + análise):
#   requests>=2.25
#   pdfplumber>=0.10  (extração de PDF)
#   pypdf>=3.9        (fallback de PDF)
#   beautifulsoup4>=4.11
#   striprtf>=0.0.26  (acórdãos RTF do eproc)
#   anthropic>=0.109  (API Claude — apenas para relatório_caso)
#   python-docx>=1.1  (exportar .docx)
```

> **Nota:** A chave `ANTHROPIC_API_KEY` deve ser configurada **somente via variável de ambiente**, nunca em arquivo ou código.

---

## 3. Arquitetura em camadas

```
orquestrador/          ← Camada 7: Fluxo completo + gates humanos
agentes/               ← Camada 6: Pesquisador e redator
qa/                    ← Camada 5: Gate anti-alucinação + rubrica
acervo/                ← Camada 4: Persistência SQLite + FTS5
leis/                  ← Camada 3: Textos legais versionados
tribunais/             ← Camada 2: Adaptadores de portais
nucleo/                ← Camada 1: HTTP, cache, catálogo, texto
```

Cada camada depende apenas das camadas abaixo. Os scripts legados (`scraper_tjsc.py`, `triagem_tjsc.py`, `relatorio_caso_tjsc.py`) são shims que continuam funcionando como antes.

---

## 4. Módulo `nucleo/`

Infraestrutura reutilizada por todos os módulos acima.

### `nucleo/http.py` — Cliente HTTP

```python
from nucleo.http import ClienteHttp, texto_resposta, classificar_resposta

cliente = ClienteHttp(
    user_agent="MeuApp/1.0 (contato: email@exemplo.com)",
    min_intervalo=2.0,    # segundos mínimos entre requisições (rate limit global)
    max_tentativas=5,     # tentativas em 429/5xx com backoff exponencial
)

resposta = cliente.get("https://exemplo.com/pagina")
html = texto_resposta(resposta)   # detecta charset automaticamente (portais legados)

# Para download de documentos (PDF/RTF):
resposta = cliente.get(url, stream=True)
formato, primeiro_chunk, iterador = classificar_resposta(resposta)
# formato: "pdf" | "rtf" | ""
```

**Backoff automático:** em HTTP 429/500/502/503/504, aguarda 4s, 8s, 16s, 32s, 60s antes de desistir.

### `nucleo/cache.py` — Escrita atômica

```python
from nucleo.cache import (
    sha256_texto, sha256_arquivo,
    carregar_json, salvar_json,
    carregar_jsonl, compactar_jsonl,
    salvar_texto, salvar_binario,
)

# Escreve arquivo de texto sem risco de corrupção (via .tmp)
salvar_texto("saida/arquivo.txt", conteudo_str)

# Escreve arquivo binário (PDF, RTF) em chunks
salvar_binario("saida/doc.pdf", primeiro_chunk, iterador_chunks)

# Salva/lê JSON atomicamente
salvar_json("dados/arquivo.json", {"chave": "valor"})
dados = carregar_json("dados/arquivo.json")  # None se não existir

# JSONL: dicionário doc_id → registro (última ocorrência vence)
registros = carregar_jsonl("catalogo.jsonl")

# Hash de conteúdo para detecção de mudanças
h = sha256_texto("texto qualquer")
h = sha256_arquivo("/caminho/para/arquivo.pdf")
```

### `nucleo/texto.py` — Extração de texto

```python
from nucleo.texto import extrair_texto, tentar_ocr

# Detecta automaticamente PDF, RTF ou HTML pelo nome do arquivo
texto = extrair_texto("decisao.pdf")
texto = extrair_texto("acordao.rtf")
texto = extrair_texto("pagina.html")

# OCR para PDFs escaneados (requer tesseract e poppler instalados)
texto = tentar_ocr("pdf_escaneado.pdf")
```

### `nucleo/catalogo.py` — Catálogo CSV

```python
from nucleo.catalogo import (
    carregar_index, regravar_index,
    reservar_base, arquivo_existente,
    slug, sanitizar_nome,
)

# Carrega catálogo existente
linhas, por_id = carregar_index("decisoes_tjsc/index.csv")

# Verifica se um arquivo já existe em disco
base_rel, formato = arquivo_existente("decisoes_tjsc/", "9a_camara/relator/proc123")

# Gera path único sem colisão
base = reservar_base("9a_camara", "Des. João Silva", "doc-001", "0001234-56.7.8.9", ocupados={})
# → "9a_camara/des_joao_silva/0001234-56_7_8_9"
```

### `nucleo/custos.py` — Rastreamento de custos da API

```python
from nucleo.custos import Contador

contador = Contador()
contador.somar("claude-opus-4-8", uso_da_resposta)  # uso vem do objeto .usage da API
print(f"Custo estimado: US$ {contador.custo():.4f}")
```

---

## 5. Módulo `leis/`

Mantém cópias locais versionadas do CPC, Código Civil e Lei de Alimentos, extraindo cada artigo individualmente.

### Uso programático

```python
from leis.planalto import LEIS, carregar_lei, artigo_texto, buscar_lei, salvar_lei
from nucleo.http import ClienteHttp

# Leis disponíveis
print(list(LEIS.keys()))
# → ["CPC", "CC", "Lei_Alimentos"]

# Verificar o cache local
dados = carregar_lei("CPC")            # None se ainda não foi baixado
dados = carregar_lei("CPC", diretorio="leis/dados")

# Recuperar texto de um artigo específico
texto = artigo_texto("CPC", "300")     # str com o texto completo do Art. 300
texto = artigo_texto("CC", 1694)       # aceita int ou str
texto = artigo_texto("CPC", "999")     # "" se não existir

# Baixar/atualizar uma lei da internet
cliente = ClienteHttp(user_agent="MeuApp/1.0", min_intervalo=2.0)
dados = buscar_lei("CPC", cliente)     # dict com lei, artigos, hash, atualizado_em

# Salvar no cache local (retorna True se conteúdo mudou)
mudou = salvar_lei(dados)
mudou = salvar_lei(dados, diretorio="/caminho/customizado")
```

### Estrutura do cache (`leis/dados/`)

```
leis/dados/
  CPC.json               ← artigos indexados por número ("300", "1", etc.)
  CPC_historico.jsonl    ← uma linha por versão (apenas quando o hash muda)
  CC.json
  CC_historico.jsonl
  Lei_Alimentos.json
  Lei_Alimentos_historico.jsonl
```

Cada `{lei}.json` contém:
```json
{
  "lei": "CPC",
  "nome": "Código de Processo Civil",
  "numero": "13.105",
  "ano": 2015,
  "url": "https://www.planalto.gov.br/...",
  "hash": "sha256 do texto completo",
  "atualizado_em": "2026-06-12",
  "vigente": true,
  "artigos": {
    "1": "Art. 1º O processo civil será ordenado...",
    "300": "Art. 300. A tutela de urgência será concedida...",
    ...
  }
}
```

### CLI

```bash
# Baixa/atualiza todas as leis
python leis/planalto.py

# Baixa somente o CPC
python leis/planalto.py CPC

# Atualiza CPC e CC em diretório alternativo
python leis/planalto.py CPC CC --out /tmp/leis_teste

# Lista estado do cache
python leis/planalto.py --listar

# Mostra texto de um artigo
python leis/planalto.py --mostrar CPC 300
python leis/planalto.py --mostrar CC 1694
```

---

## 6. Módulo `tribunais/`

### Hierarquia de adaptadores

```
AdaptadorTribunal (ABC)          ← tribunais/base.py
├── AdaptadorTjsc                ← tribunais/tjsc.py  (POST, ISO-8859-1)
└── AdaptadorEsaj (intermédio)   ← tribunais/esaj.py  (GET, conversationId)
    ├── AdaptadorTjsp            ← tribunais/tjsp.py
    └── AdaptadorTjms            ← tribunais/tjms.py
```

### Contrato comum (`AdaptadorTribunal`)

Todos os adaptadores expõem os mesmos 5 métodos:

| Método | Descrição |
|--------|-----------|
| `verificar_robots()` | Lê `robots.txt`; retorna `True` se acesso permitido |
| `estabelecer_sessao()` | Handshake inicial (cookies, `conversationId` no eSAJ) |
| `parsear_resultados(html)` | Parse de uma página → `(registros, total)` |
| `buscar(relator, params, ps=50)` | Iterador que pagina o portal |
| `baixar_inteiro_teor(doc_id, tipo, destino)` | Baixa PDF/RTF → `InteiroTeor` |

### Usando o AdaptadorTjsc

```python
from tribunais.tjsc import AdaptadorTjsc

adaptador = AdaptadorTjsc(
    user_agent="MeuApp/1.0 (contato: email@ex.com)",
    min_intervalo=2.0,
    max_tentativas=5,
    ps=50,          # resultados por página
    max_paginas=400,
)

# 1. Verificar permissão
if not adaptador.verificar_robots():
    print("Acesso bloqueado por robots.txt")
    exit()

# 2. Estabelecer sessão (não necessário no TJSC, mas boa prática)
adaptador.estabelecer_sessao()

# 3. Buscar acórdãos (gerador que pagina automaticamente)
for registros, total, pagina, html_bruto in adaptador.buscar(
    relator="Des. Nome Relator",
    params={"pesquisa.buscaLivre": "alimentos gravídicos"},
):
    print(f"Página {pagina}: {len(registros)} acórdãos de {total} total")
    for r in registros:
        print(r["doc_id"], r.get("ementa", "")[:80])

# 4. Baixar inteiro teor
from tribunais.base import InteiroTeor
it: InteiroTeor = adaptador.baixar_inteiro_teor(
    doc_id="abc123",    # rowid no TJSC
    tipo="acordao",
    base_destino="decisoes_tjsc/9a_camara/relator_x",
)
print(it.caminho, it.formato)  # "...acord_abc123.pdf", "pdf"
```

### Usando os adaptadores eSAJ (TJSP / TJMS)

```python
from tribunais.tjsp import AdaptadorTjsp
from tribunais.tjms import AdaptadorTjms

# TJSP
adaptador = AdaptadorTjsp(
    user_agent="MeuApp/1.0",
    min_intervalo=2.0,
)

# ATENÇÃO: no eSAJ, estabelecer_sessao() é OBRIGATÓRIO antes de buscar()
# Sem isso, buscar() retorna 0 resultados
adaptador.estabelecer_sessao()

for registros, total, pagina, _ in adaptador.buscar("Des. Relator", {}):
    for r in registros:
        # doc_id no eSAJ: "cdAcordao:cdForo" (ex.: "1234567:28")
        print(r["doc_id"])

# TJMS — idêntico ao TJSP, diferente apenas na URL base
adaptador_ms = AdaptadorTjms(user_agent="MeuApp/1.0")
adaptador_ms.estabelecer_sessao()
```

### `InteiroTeor` — estrutura de retorno

```python
from tribunais.base import InteiroTeor

# Campos
it.caminho   # str: caminho absoluto do arquivo salvo em disco
it.formato   # str: "pdf" | "rtf" | "html" | ""
it.url       # str: URL de onde o arquivo foi baixado
```

---

## 7. Módulo `acervo/`

Banco de dados SQLite com FTS5 para armazenar e consultar decisões coletadas.

### Uso básico

```python
from acervo.db import Acervo

# Abre (ou cria) o banco
with Acervo("acervo.db") as db:

    # Inserir uma decisão (retorna True se nova, False se duplicata)
    nova = db.inserir({
        "doc_id":          "1234567:28",
        "tribunal":        "tjsp",
        "relator":         "Des. Fulano de Tal",
        "tipo":            "Acórdão",
        "data_julgamento": "2024-03-15",
        "ementa":          "Alimentos. Binômio necessidade-possibilidade...",
        "url":             "https://esaj.tjsp.jus.br/...",
    })

    # Registrar o arquivo baixado
    db.registrar_download(
        doc_id="1234567:28",
        caminho="/caminho/local/decisao.pdf",
        formato="pdf",
        hash_arquivo="sha256_do_arquivo",
    )

    # Buscar por texto na ementa (FTS5)
    resultados = db.buscar_texto("alimentos gravídicos")
    resultados = db.buscar_texto("tutela urgência", limite=20)

    # Buscar por filtros estruturados
    resultados = db.buscar_filtros(tribunal="tjsc")
    resultados = db.buscar_filtros(relator="Des. Nome", data_inicio="2024-01-01")
    resultados = db.buscar_filtros(
        tribunal="tjsp",
        data_inicio="2023-06-01",
        data_fim="2024-06-01",
        limite=100,
    )

    # Obter decisão específica
    registro = db.obter("1234567:28")    # dict ou None

    # Listar arquivos baixados de uma decisão
    downloads = db.downloads("1234567:28")

    # Estatísticas
    s = db.stats()
    # → {"total": 1500, "por_tribunal": {"tjsc": 800, "tjsp": 700}, "downloads": 300}

    # Exportar para JSONL (para análise externa, ML, etc.)
    n = db.exportar_jsonl("exportacao.jsonl")
    n = db.exportar_jsonl("tjsc_2024.jsonl", tribunal="tjsc", data_inicio="2024-01-01")

    # Excluir decisão (e seus downloads por cascade)
    excluiu = db.excluir("doc_id_aqui")
```

### Schema do banco

| Tabela | Descrição |
|--------|-----------|
| `decisoes` | Metadados de cada acórdão (doc_id único) |
| `downloads` | Arquivos baixados (referencia decisoes) |
| `fts_decisoes` | Índice FTS5 na ementa (atualizado por triggers) |

---

## 8. Módulo `qa/`

Valida citações legais antes de usar em peças e atribui um score de qualidade ao rascunho.

### Gate anti-alucinação (`qa/gate.py`)

Três checks independentes, cada um retorna `{"ok": bool, "msg": str, ...}`:

```python
from qa.gate import (
    verificar_existencia,
    verificar_verbatim,
    verificar_vigencia,
    verificar_citacao,   # consolida os 3
)

# Check 1: o artigo existe no cache local?
r = verificar_existencia("CPC", "300")
r = verificar_existencia("CC", 1694, diretorio="leis/dados")
# → {"check": "existencia", "ok": True, "lei": "CPC", "artigo": "300", "msg": ""}

# Check 2: o texto citado é fiel ao original? (Jaccard de tokens, limiar 0.85)
r = verificar_verbatim(
    "CPC", "300",
    "A tutela de urgência será concedida quando houver elementos...",
    limiar=0.85,          # padrão; ajustável
)
# → {"check": "verbatim", "ok": True, "similaridade": 0.923, "limiar": 0.85, "msg": ""}

# Check 3: o cache foi atualizado nos últimos 30 dias?
r = verificar_vigencia("CPC", max_idade_dias=30)
# → {"check": "vigencia", "ok": True, "atualizado_em": "2026-06-12", "idade_dias": 0, ...}

# Consolidado: executa os 3 e retorna resultado único
r = verificar_citacao(
    "CPC", "300",
    "A tutela de urgência será concedida quando houver elementos...",
    limiar_verbatim=0.85,
    max_idade_dias=30,
    diretorio="leis/dados",
)
# → {"aprovado": True, "lei": "CPC", "artigo": "300",
#    "checks": [...], "reprovados": []}
```

**Interpretação da similaridade Jaccard:**
- `1.0` — citação idêntica ao texto armazenado
- `≥ 0.85` — aprovado (pequenas variações tipográficas/de parágrafo aceitas)
- `< 0.85` — reprovado (citação imprecisa ou artigo errado)

### Rubrica de qualidade (`qa/rubrica.py`)

Avalia uma peça jurídica em 4 dimensões, com pontuação 0–10:

```python
from qa.rubrica import avaliar_peca

resultado = avaliar_peca(
    texto_peca,                  # str: texto completo da peça
    citacoes=[                   # list[(lei, artigo, trecho_citado)]
        ("CPC", "300", "A tutela de urgência..."),
        ("CC", "1694", "Podem os parentes..."),
    ],
    fundamentos_esperados=[      # palavras/frases que devem aparecer no texto
        "fumus boni iuris",
        "periculum in mora",
        "alimentos",
    ],
    secoes_obrigatorias=[        # seções que devem constar na peça
        "DOS FATOS",
        "DO DIREITO",
        "DOS PEDIDOS",
    ],
    limiar=7.0,                  # score mínimo para aprovação (padrão: 7.0)
    diretorio="leis/dados",
)

print(resultado["aprovada"])     # True / False
print(resultado["score"])        # 0.0–10.0

# Dimensões (cada uma vale 0–10, ponderadas)
d = resultado["dimensoes"]
print(d["citacoes_verificadas"])   # 30% do score
print(d["base_legal"])             # 30% — fundamentos no texto
print(d["consistencia_factual"])   # 20% — sem contradições detectadas
print(d["qualidade_formal"])       # 20% — seções obrigatórias presentes

# Detalhes do que foi encontrado/faltou
det = resultado["detalhes"]
print(det["citacoes"])             # resultado de cada verificar_citacao()
print(det["fundamentos_ausentes"]) # fundamentos que não aparecem no texto
print(det["secoes_ausentes"])      # seções obrigatórias faltando
print(det["alertas_consistencia"]) # possíveis contradições detectadas
```

**Fórmula:**
```
score = (citacoes × 0.30) + (base_legal × 0.30) + (consistencia × 0.20) + (formal × 0.20)
```

---

## 9. Módulo `agentes/`

Agentes autônomos que encapsulam tarefas específicas com contrato JSON padronizado.

### Envelopes de comunicação (`agentes/base.py`)

```python
from agentes.base import Contexto, ResultadoAgente

# Entrada para qualquer agente
ctx = Contexto(
    tarefa="redigir",
    dados={"tipo_peca": "tutela_antecipada", "fatos": "..."},
    historico=[],      # mensagens anteriores (opcional)
    meta={},           # metadados extras (opcional)
)

# Serialização / deserialização
json_str = ctx.serializar()
ctx2 = Contexto.desserializar(json_str)

# Saída de qualquer agente
resultado = ResultadoAgente(
    agente="redator",
    tarefa="redigir",
    status="ok",               # "ok" | "erro" | "aguardando_aprovacao"
    saida={"rascunho": "..."},
    requer_aprovacao=False,
)
```

### AgentePesquisador

Busca jurisprudência em um ou mais tribunais e salva no acervo.

```python
from acervo.db import Acervo
from agentes.pesquisador import AgentePesquisador
from agentes.base import Contexto

with Acervo("acervo.db") as db:
    pesquisador = AgentePesquisador(db, min_intervalo=2.0)

    ctx = Contexto(
        tarefa="pesquisa",
        dados={
            "relator":      "Des. Nome do Relator",  # opcional
            "termos":       ["alimentos", "tutela urgência"],  # filtro pós-busca
            "tribunais":    ["tjsc", "tjsp"],         # padrão: todos
            "params":       {},                       # parâmetros extras do adaptador
            "max_decisoes": 50,
        },
    )

    resultado = pesquisador.executar(ctx)
    if resultado.status == "ok":
        print(f"Encontradas: {resultado.saida['total_encontrado']} decisões")
        print(f"Tribunais consultados: {resultado.saida['tribunais_consultados']}")
        for decisao in resultado.saida["decisoes"]:
            print(decisao["doc_id"], decisao["tribunal"])
```

**Tribunais suportados:** `"tjsc"`, `"tjsp"`, `"tjms"`

### AgenteRedator

Gera rascunhos de peças jurídicas a partir de templates. **Toda saída requer aprovação humana** (`requer_aprovacao=True`).

```python
from agentes.redator import AgenteRedator
from agentes.base import Contexto

redator = AgenteRedator()

ctx = Contexto(
    tarefa="redigir",
    dados={
        "tipo_peca":  "tutela_antecipada",   # ou "peticao_alimentos"
        "fatos":      "A autora, mãe de menor de 3 anos, comprova...",
        "pedidos":    [
            "a concessão de tutela de urgência",
            "a fixação de alimentos provisórios em 30% do salário mínimo",
        ],
        "requerente": "Maria da Silva",
        "requerido":  "João Souza",
        "decisoes":   [],   # jurisprudência do pesquisador (opcional)
    },
)

resultado = redator.executar(ctx)
# status: "aguardando_aprovacao" (sempre requer revisão humana)

rascunho = resultado.saida["rascunho"]      # texto completo da peça
citacoes = resultado.saida["citacoes"]      # [[lei, artigo, trecho], ...]
secoes   = resultado.saida["secoes"]        # ["DOS FATOS", "DO DIREITO", "DOS PEDIDOS"]
```

**Tipos de peça disponíveis:**

| `tipo_peca` | Fundamento legal | Citações automáticas |
|------------|-----------------|----------------------|
| `tutela_antecipada` | CPC art. 300 | CPC 300 |
| `peticao_alimentos` | CC arts. 1.694/1.695 + Lei 5.478/68 | CC 1694, CC 1695 |

---

## 10. Módulo `orquestrador/`

Coordena o fluxo completo com dois gates de aprovação humana obrigatórios.

### Fluxo de execução

```
1. AgentePesquisador.executar()
   └─ erro → RetornaFluxo(status="erro", etapa_final="pesquisa")

2. AgenteRedator.executar()
   └─ erro → RetornaFluxo(status="erro", etapa_final="redacao")

3. Gate humano: "Aprovar rascunho?"
   └─ não → RetornaFluxo(status="reprovado", etapa_final="aprovacao_rascunho")

4. qa.rubrica.avaliar_peca() — score >= limiar_qa (padrão 7.0)
   └─ reprovado → RetornaFluxo(status="reprovado", etapa_final="qa")

5. Gate humano: "Aprovar peça final?"
   └─ não → RetornaFluxo(status="reprovado", etapa_final="aprovacao_final")

6. RetornaFluxo(status="aprovado", etapa_final="concluido")
```

### Uso

```python
from acervo.db import Acervo
from orquestrador.fluxo import Orquestrador

with Acervo("acervo.db") as db:
    orq = Orquestrador(
        db,
        diretorio_leis="leis/dados",  # cache das leis (para o gate QA)
        min_intervalo=2.0,
        # aprovador: função chamada nos gates humanos
        # padrão: pergunta interativamente no terminal [s/N]
    )

    resultado = orq.executar(
        tipo_peca="tutela_antecipada",
        fatos="A autora, gestante, comprova...",
        pedidos=["fixação de alimentos gravídicos em 20% do salário mínimo"],
        requerente="Maria da Silva",
        requerido="João Souza",
        termos=["alimentos", "fumus boni iuris"],  # fundamentos esperados (QA)
        tribunais=["tjsc", "tjsp"],                # onde pesquisar
        relator="",                                # "" = qualquer relator
        limiar_qa=7.0,                             # score mínimo (0–10)
    )

    print(resultado.status)       # "aprovado" | "reprovado" | "erro"
    print(resultado.etapa_final)  # onde o fluxo encerrou
    print(resultado.rascunho)     # texto da peça (se chegou até a redação)
    print(resultado.score_qa)     # pontuação da rubrica (0.0–10.0)
```

### Aprovador customizado (para integração e testes)

O parâmetro `aprovador` aceita qualquer função `(etapa: str, dados: dict) -> bool`:

```python
# Exemplo: aprovação automática (cuidado em produção!)
orq = Orquestrador(db, aprovador=lambda etapa, dados: True)

# Exemplo: log + aprovação manual via e-mail/Slack
def meu_aprovador(etapa: str, dados: dict) -> bool:
    enviar_notificacao(etapa, dados)
    return aguardar_confirmacao_externa(etapa)

orq = Orquestrador(db, aprovador=meu_aprovador)

# Serializar resultado para armazenar ou transmitir
json_resultado = resultado.serializar()
```

### Validação de contratos (`orquestrador/contrato.py`)

```python
from orquestrador.contrato import validar_contexto, TIPOS_PECA, TRIBUNAIS_SUPORTADOS

print(TIPOS_PECA)           # ("tutela_antecipada", "peticao_alimentos")
print(TRIBUNAIS_SUPORTADOS) # ("tjsc", "tjsp", "tjms")

erros = validar_contexto("redigir", {"tipo_peca": "tutela_antecipada"})
# → ["campo obrigatório ausente: 'fatos'"]

erros = validar_contexto("redigir", {"tipo_peca": "tutela_antecipada", "fatos": "..."})
# → []  (válido)
```

---

## 11. Scripts de linha de comando

### `scraper_tjsc.py` — Coleta automatizada do TJSC

```bash
# Busca e baixa todos os acórdãos dos relatores configurados
python scraper_tjsc.py

# Apenas busca e cataloga (sem baixar arquivos)
python scraper_tjsc.py --dry-run

# Muda o diretório de saída
python scraper_tjsc.py --out /caminho/alternativo

# Resultado:
# decisoes_tjsc/
#   9a_camara/<relator_slug>/<proc>.pdf
#   10a_camara/<relator_slug>/<proc>.rtf
#   index.csv                     ← catálogo de todas as decisões
#   _raw_html/                    ← snapshots HTML para auditoria
#   run.log                       ← log detalhado da execução
```

### `triagem_tjsc.py` — Triagem por relevância

```bash
python triagem_tjsc.py --dir decisoes_tjsc/
```

Lê o `index.csv`, extrai texto dos PDFs/RTFs, aplica critérios de relevância.

### `relatorio_caso_tjsc.py` — Relatório do caso concreto

```bash
# Requer ANTHROPIC_API_KEY no ambiente
export ANTHROPIC_API_KEY=sk-ant-...
python relatorio_caso_tjsc.py --caso peça_inicial.pdf --acervo decisoes_tjsc/
```

Cruza as peças do caso concreto com os acórdãos coletados via API do Claude.

### `leis/planalto.py` — Gestão do repositório de leis

```bash
python leis/planalto.py --listar              # estado do cache
python leis/planalto.py                       # atualiza todas
python leis/planalto.py CPC CC                # atualiza selecionadas
python leis/planalto.py --mostrar CPC 300     # imprime artigo
python leis/planalto.py --out /tmp/leis_test  # diretório alternativo
```

---

## 12. Fluxo de trabalho típico

### Cenário: pesquisa + elaboração de tutela de urgência

```bash
# Passo 1: Atualizar o repositório de leis (uma vez por mês)
python leis/planalto.py
python leis/planalto.py --listar   # confirma que está atualizado

# Passo 2: Coletar jurisprudência do TJSC
python scraper_tjsc.py --dry-run   # testa sem baixar
python scraper_tjsc.py             # coleta completa

# Passo 3: (via Python) executar o fluxo completo
```

```python
from acervo.db import Acervo
from agentes.pesquisador import AgentePesquisador
from agentes.base import Contexto
from orquestrador.fluxo import Orquestrador

# Opção A: Fluxo manual (controle total)
with Acervo("acervo.db") as db:

    # Importar decisões do index.csv para o acervo SQLite
    import csv
    with open("decisoes_tjsc/index.csv") as f:
        for linha in csv.DictReader(f):
            if linha.get("doc_id"):
                db.inserir({
                    "doc_id":          linha["doc_id"],
                    "tribunal":        "tjsc",
                    "relator":         linha["relator"],
                    "tipo":            linha["classe"],
                    "data_julgamento": linha["data_julgamento"],
                    "ementa":          "",  # preencher se disponível
                    "url":             linha["url_integra"],
                })

    # Buscar no acervo
    decisoes = db.buscar_texto("alimentos gravídicos tutela urgência")
    stats = db.stats()
    print(f"Acervo: {stats['total']} decisões")

# Opção B: Fluxo orquestrado completo
with Acervo("acervo.db") as db:
    orq = Orquestrador(db, diretorio_leis="leis/dados")

    resultado = orq.executar(
        tipo_peca="tutela_antecipada",
        fatos="""
            A autora está grávida de 6 meses. O genitor, apesar de intimado,
            não contribui com as despesas gestacionais. Renda mensal comprovada
            de R$ 5.000,00 (cinco mil reais).
        """,
        pedidos=[
            "fixação de alimentos gravídicos provisórios no valor de R$ 1.500,00/mês",
            "citação do réu para contestar no prazo legal",
        ],
        requerente="Maria da Silva",
        requerido="João Souza",
        termos=["alimentos", "fumus boni iuris", "urgência"],
        tribunais=["tjsc"],
    )

    if resultado.status == "aprovado":
        with open("tutela_urgencia_draft.txt", "w") as f:
            f.write(resultado.rascunho)
        print(f"Peça aprovada! Score QA: {resultado.score_qa:.1f}/10")
    else:
        print(f"Fluxo encerrado em: {resultado.etapa_final}")
        print(f"Motivo: {resultado.mensagem}")
```

---

## 13. Variáveis de ambiente

| Variável | Padrão | Descrição |
|----------|--------|-----------|
| `ANTHROPIC_API_KEY` | — | **Obrigatória** para `relatorio_caso_tjsc.py`. Nunca commitada. |
| `TJSC_BASE` | `https://busca.tjsc.jus.br/jurisprudencia` | URL base do portal TJSC (útil para testes) |
| `TJSC_MIN_INTERVALO` | `2` | Segundos mínimos entre requisições ao TJSC |
| `PLANALTO_BASE` | `https://www.planalto.gov.br` | URL base do planalto.gov.br |
| `PLANALTO_MIN_INTERVALO` | `2` | Segundos mínimos entre requisições ao planalto |

**Configurar no Linux/Mac:**
```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...
export TJSC_MIN_INTERVALO=3   # mais conservador
```

**Configurar em `.env` (não commitado):**
```env
ANTHROPIC_API_KEY=sk-ant-api03-...
TJSC_MIN_INTERVALO=2
```
```python
from dotenv import load_dotenv
load_dotenv()
```

---

## 14. Segurança e boas práticas

### O que nunca deve ser commitado

| Item | Motivo |
|------|--------|
| `ANTHROPIC_API_KEY` ou qualquer chave de API | Comprometimento da conta |
| Peças do caso concreto | Dados do cliente, sigilo profissional |
| Processos em segredo de justiça | Restrição legal |
| `acervo.db`, `*.db` | Pode conter decisões não públicas |
| `leis/dados/` | Cache regenerável; evita conflitos de merge |

O `.gitignore` já exclui: `leis/dados/`, `*.db`, `acervo.db`, `decisoes_tjsc/`.

### Rate limiting e robots.txt

Todos os adaptadores:
- Verificam `robots.txt` antes de qualquer requisição; abortam se bloqueados
- Respeitam intervalo mínimo de **2 segundos** entre requisições (global, não por conexão)
- Aplicam backoff exponencial em erros 429/5xx
- Identificam-se com User-Agent contendo nome do projeto e e-mail de contato

### Gate humano obrigatório

O `Orquestrador` para o fluxo em dois pontos para aprovação humana:
1. Antes de enviar o rascunho para o QA
2. Após aprovação do QA, antes de entregar a peça

**Nunca remova os gates humanos** sem revisão criteriosa — peças jurídicas têm consequências legais reais.

---

## 15. Solução de problemas

### `ModuleNotFoundError: No module named 'requests'`
```bash
pip install requests
# ou
pip install -r requirements.txt
```

### `robots.txt bloqueia ...`
O portal bloqueou o acesso. Verifique manualmente o `robots.txt` do portal. Não tente contornar.

### `estabelecer_sessao()` não chamado (eSAJ retorna 0 resultados)
```python
# ERRADO
for registros, ... in adaptador.buscar(...):  # retorna vazio!

# CORRETO
adaptador.estabelecer_sessao()               # ← obrigatório
for registros, ... in adaptador.buscar(...):
```

### Gate verbatim falha mesmo com texto correto
Reduzir o limiar ou verificar se o cache local está desatualizado:
```python
# Verificar versão do cache
from leis.planalto import carregar_lei
dados = carregar_lei("CPC")
print(dados["atualizado_em"])     # data da última atualização
print(dados["artigos"]["300"])    # texto armazenado

# Atualizar se necessário
python leis/planalto.py CPC
```

### Score QA abaixo de 7.0
```python
# Inspecionar o que falhou
resultado = avaliar_peca(texto, citacoes=..., ...)
print(resultado["dimensoes"])          # qual dimensão está baixa?
print(resultado["detalhes"])           # o que está faltando?
```

Causas comuns:
- `citacoes_verificadas` baixo → citação imprecisa ou lei desatualizada
- `base_legal` baixo → fundamentos esperados não mencionados no texto
- `qualidade_formal` baixo → seções obrigatórias ausentes (ex.: falta "DOS PEDIDOS")

### `sqlite3.OperationalError: no such module: fts5`
O SQLite foi compilado sem FTS5 (raro em CPython padrão). Solução:
```bash
# Verificar
python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(x)')"

# Se falhar, reinstale Python via pyenv com SQLite completo
```

---

## Referência rápida de imports

```python
# Infraestrutura
from nucleo.http   import ClienteHttp, texto_resposta, classificar_resposta
from nucleo.cache  import sha256_texto, carregar_json, salvar_json, salvar_texto, salvar_binario
from nucleo.texto  import extrair_texto
from nucleo.custos import Contador

# Tribunais
from tribunais.base import AdaptadorTribunal, InteiroTeor
from tribunais.tjsc import AdaptadorTjsc
from tribunais.tjsp import AdaptadorTjsp
from tribunais.tjms import AdaptadorTjms

# Leis
from leis.planalto import LEIS, carregar_lei, artigo_texto, buscar_lei, salvar_lei

# Acervo
from acervo.db import Acervo

# QA
from qa.gate   import verificar_existencia, verificar_verbatim, verificar_vigencia, verificar_citacao
from qa.rubrica import avaliar_peca

# Agentes
from agentes.base       import AgenteBase, Contexto, ResultadoAgente
from agentes.pesquisador import AgentePesquisador
from agentes.redator    import AgenteRedator

# Orquestrador
from orquestrador.fluxo    import Orquestrador, ResultadoFluxo
from orquestrador.contrato import validar_contexto, TIPOS_PECA, TRIBUNAIS_SUPORTADOS
```

---

*Manual gerado em 2026-06-12 — branch `claude/eager-edison-e4htey`*
