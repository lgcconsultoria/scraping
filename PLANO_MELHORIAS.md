# Plano de Melhorias — Plataforma de Pesquisa Jurisprudencial

> Evolução do conjunto de scripts atual (raspador TJSC + triagem + relatório de caso)
> para uma **plataforma web completa de pesquisa e análise de jurisprudência**, com
> banco de dados, busca facetada/semântica, motor de análise de caso via API do
> Claude e frontend próprio.

---

## 1. Onde estamos e para onde vamos

### Hoje (v1 — scripts CLI)

| Peça | O que faz | Limitação principal |
|---|---|---|
| `scraper_tjsc.py` | Busca e baixa acórdãos das 9ª/10ª Câmaras (8 relatores, eixos fixos em código) | Catálogo em CSV; alvos e temas hardcoded; só TJSC |
| `triagem_tjsc.py` | Classifica FAVORÁVEL/CONTRÁRIA/NEUTRA por palavra-chave | Sobrescreve artefatos; dicionários no código; sem histórico |
| `relatorio_caso_tjsc.py` | Cruza a peça do caso com o corpus via Claude (4 estágios) | Varre o corpus inteiro (sem retrieval); cache em JSONL; sem UI |

Pontos fortes a **preservar**: idempotência, rate-limit respeitoso, testes offline
contra portal falso, pipeline de IA com cache por hash e uso do modelo certo por
estágio (pesado × leve).

### Amanhã (v2 — plataforma)

```
┌────────────┐   ┌──────────────┐   ┌───────────────────┐   ┌──────────────┐
│  COLETA    │──▶│  BANCO DE    │──▶│  ENRIQUECIMENTO   │──▶│  CONSULTA    │
│ (scrapers  │   │  DADOS       │   │ texto, área,      │   │ facetada +   │
│  agendáveis)│  │ (PostgreSQL) │   │ assunto, embedding │  │ semântica    │
└────────────┘   └──────┬───────┘   └───────────────────┘   └──────┬───────┘
                        │                                          │
                        ▼                                          ▼
                 ┌──────────────────────────────┐          ┌──────────────┐
                 │  ANÁLISE DE CASO (RAG+Claude)│◀────────▶│  FRONTEND    │
                 │  peça → perfil → retrieval → │          │  (Next.js)   │
                 │  aplicação → relatório       │          └──────────────┘
                 └──────────────────────────────┘
```

Duas vertentes, como solicitado:

1. **Acervo**: coletar jurisprudência continuamente e salvar em banco de dados,
   organizada por tribunal, órgão julgador (câmara/turma), relator, área, assunto,
   classe e data — pesquisável por qualquer combinação desses filtros + texto livre
   + busca semântica.
2. **Análise de caso**: dado um caso concreto (peça em md/txt/docx/pdf), encontrar
   no banco as jurisprudências aplicáveis (retrieval híbrido) e gerar o relatório
   de precedentes com a API do Claude — sem varrer o corpus inteiro.

---

## 2. Arquitetura proposta

### Stack

| Camada | Escolha | Por quê |
|---|---|---|
| Banco | **PostgreSQL 16 + pgvector** | Um único banco cobre relacional, full-text em português (`tsvector` c/ dicionário `portuguese` + `unaccent`) e busca vetorial (pgvector). Evita manter Elasticsearch à parte. |
| Backend | **Python + FastAPI + SQLAlchemy + Alembic** | Reaproveita 100% do código existente (parser do portal, extração RTF/PDF, pipeline Claude). OpenAPI grátis para o frontend. Alembic versiona o schema. |
| Jobs assíncronos | **Fila simples no próprio Postgres + worker** (evoluível p/ Celery+Redis) | Coletas e análises são demoradas; o frontend acompanha por polling/SSE. Começar sem Redis reduz peças móveis. |
| Frontend | **Next.js + TypeScript + Tailwind + shadcn/ui** | SPA/SSR moderna, componentes prontos (tabelas, filtros facetados, formulários), fácil de hospedar. |
| Arquivos (PDF/RTF/HTML) | Filesystem com caminho no banco (`storage/`), interface `Storage` p/ trocar por S3/MinIO depois | Os inteiros teores continuam em disco; o banco guarda metadados + texto extraído + hash. |
| Infra dev | **docker-compose** (postgres + api + worker + web) | `docker compose up` levanta tudo; scripts atuais continuam rodáveis standalone durante a transição. |
| IA | **API do Claude** (`claude-opus-4-8` raciocínio, `claude-sonnet-4-6`/`claude-haiku-4-5` extração/classificação em massa) + **Batches API** (−50% de custo p/ lotes) | Mantém a filosofia atual de modelo certo por estágio; Batches derruba pela metade o custo da extração/classificação em massa. |
| Embeddings | **Voyage AI `voyage-law-2`** (embeddings jurídicos, parceiro recomendado pela Anthropic) *ou* modelo local `intfloat/multilingual-e5-large` (custo zero) | Decidir na Fase 2; a interface de embedding fica plugável. |

### Estrutura do repositório (monorepo)

```
scraping/
├── backend/
│   ├── app/
│   │   ├── api/            # routers FastAPI (acordaos, busca, casos, coletas, auth)
│   │   ├── core/           # config, segurança, storage
│   │   ├── db/             # models SQLAlchemy, sessão, migrations (alembic/)
│   │   ├── scrapers/       # tjsc.py (adaptado do scraper atual) + base.py (interface p/ outros tribunais)
│   │   ├── ingest/         # extração de texto (pdf/rtf/html), normalização, dedupe
│   │   ├── enrich/         # classificação área/assunto, triagem, embeddings
│   │   ├── analysis/       # pipeline do caso (perfil → retrieval → aplicação → síntese)
│   │   └── workers/        # executor de jobs (coleta, enriquecimento, análise)
│   ├── tests/              # migra e amplia a suíte offline atual
│   └── pyproject.toml
├── frontend/               # Next.js
├── docker-compose.yml
├── scripts/                # migrar_csv_para_db.py, utilidades
└── legacy/                 # scripts v1 preservados até o fim da transição
```

---

## 3. Modelo de dados

Desenhado para **não se limitar ao TJSC** — tribunal é dado, não constante.

```sql
tribunais            (id, sigla, nome, uf)                     -- TJSC hoje; TJSP, STJ... depois
orgaos_julgadores    (id, tribunal_id, nome, tipo)             -- "9ª Câmara de Direito Civil"; tipo: câmara/turma/seção
relatores            (id, tribunal_id, nome, nome_normalizado, grafias_alternativas[])
areas                (id, nome, pai_id)                        -- Direito de Família > Alimentos (hierárquica)
assuntos             (id, codigo_cnj, nome, pai_id)            -- alinhada à Tabela Unificada de Assuntos do CNJ
classes              (id, codigo_cnj, nome)                    -- Agravo de Instrumento, Apelação...

acordaos (
  id, tribunal_id, orgao_julgador_id, relator_id, classe_id,
  numero_processo, comarca, data_julgamento, data_publicacao,
  doc_id_portal UNIQUE, url_integra,
  ementa TEXT, texto_integral TEXT, resultado,                 -- provido/desprovido/parcial (extraído)
  arquivo_path, formato, hash_conteudo, precisa_ocr BOOL,
  ts tsvector GENERATED (ementa + texto),                      -- índice GIN full-text português
  criado_em, atualizado_em
)
acordao_assuntos     (acordao_id, assunto_id, origem)          -- N:N; origem: portal|regra|llm|manual
acordao_areas        (acordao_id, area_id, origem)

acordao_chunks       (id, acordao_id, ordem, texto, embedding vector(1024))  -- pgvector, índice HNSW

-- Coleta configurável (substitui RELATORES/EIXOS hardcoded)
temas_pesquisa       (id, nome, tribunal_id, params_busca JSONB, relatores[], ativo, agenda_cron)
coletas              (id, tema_id, status, iniciada_em, terminada_em, stats JSONB, log)

-- Triagem versionada (substitui triagem.csv)
criterios_triagem    (id, nome, dicionario JSONB, limiares JSONB, versao)
triagens             (id, acordao_id, criterio_id, score, classe, disparos JSONB, executada_em)

-- Análise de caso
casos                (id, usuario_id, titulo, arquivo_peca, hash_peca, perfil JSONB, criado_em)
extracoes            (id, acordao_id, modelo, payload JSONB, hash_texto)      -- cache LLM independente do caso
analises             (id, caso_id, status, params JSONB, custo_usd, criada_em, concluida_em)
analise_itens        (id, analise_id, acordao_id, posicao, aplicabilidade, payload JSONB)
relatorios           (id, analise_id, markdown, docx_path)

usuarios             (id, email, senha_hash, papel)            -- admin | pesquisador
```

Decisões importantes:

- **Texto integral no banco** — extraído uma única vez na ingestão (a lógica de
  `triagem_tjsc.extrair_texto` migra para `ingest/`). Triagem, embeddings e LLM
  passam a ler do banco, nunca mais do arquivo.
- **Assuntos = Tabela CNJ** — usar a Tabela Unificada de Assuntos do CNJ como
  taxonomia canônica (hierárquica, com código oficial) em vez de inventar uma.
  O portal do TJSC nem sempre entrega o assunto; a classificação automática
  (item 5) preenche a lacuna.
- **`doc_id_portal` UNIQUE** preserva a idempotência atual: reprocessar nunca duplica.
- **`extracoes` desacopladas do caso** (como hoje): trocar a peça não invalida a
  leitura dos acórdãos — só perfil e aplicações são refeitos.

---

## 4. Coleta (evolução do scraper)

1. **Extrair o motor do scraper para `scrapers/tjsc.py`** mantendo intactos:
   cliente HTTP com rate-limit ≥2s, backoff, robots.txt, parser dos resultados,
   os 3 caminhos de download (PDF/RTF/HTML) e os snapshots `_raw_html` (passam a
   ser gravados por coleta, para auditoria).
2. **Interface `ScraperBase`** (buscar, paginar, baixar_integra) para plugar
   outros tribunais no futuro sem tocar no resto do sistema.
3. **Temas de pesquisa no banco** — o que hoje é `RELATORES` + `EIXOS` vira
   registros em `temas_pesquisa`, criáveis/editáveis pela UI (campos do portal:
   `q`, `frase`, `classe`, `datainicial`... em JSONB validado).
4. **Coletas como jobs**: disparadas pela UI ("Executar agora") ou por agenda
   (cron por tema). Cada execução grava linha em `coletas` com stats (novos,
   pulados, falhas) e log — hoje isso se perde no `run.log`.
5. **Ingestão no ato**: ao baixar, extrair texto (com fila separada p/ OCR),
   calcular hash, gravar acórdão + disparar enriquecimento.
6. **Migração do acervo atual**: script único que lê `decisoes_tjsc/index.csv` +
   arquivos e popula o banco (nada do que já foi baixado se perde).

---

## 5. Enriquecimento e classificação

Três camadas, da mais barata para a mais cara:

1. **Regras determinísticas (custo zero)** — resultado (provido/desprovido) por
   regex na parte dispositiva; classe/órgão normalizados; a triagem por
   palavra-chave atual vira `criterios_triagem` versionados no banco (ajustar
   peso ≠ mexer em código).
2. **Classificação por LLM em lote (Batches API, −50%)** — para cada acórdão novo,
   uma chamada barata (`claude-haiku-4-5` ou `claude-sonnet-4-6`) com **structured
   outputs** (`output_config.format` com JSON Schema) devolvendo: área, assuntos
   CNJ (da taxonomia, não texto livre), resultado, síntese da ementa em 1 frase e
   partes-tema. Idempotente por `hash_texto` — mesmo padrão de cache de hoje.
3. **Embeddings** — chunking do inteiro teor (~800 tokens com overlap) →
   `acordao_chunks.embedding` (pgvector/HNSW). Recalcular só quando o texto mudar.

## 6. Busca

- **Facetada**: `GET /api/acordaos?tribunal=&orgao=&relator=&classe=&area=&assunto=&data_ini=&data_fim=&resultado=&classe_triagem=` com contagem por faceta (para a UI mostrar "9ª Câmara (142)").
- **Full-text**: `?q=...` via `tsvector` português com `unaccent`, ranking `ts_rank`, destaque de trecho (`ts_headline`).
- **Semântica**: `?q_semantica=...` → embedding da consulta → k-NN nos chunks.
- **Híbrida (padrão da UI)**: full-text + vetorial combinados por Reciprocal Rank
  Fusion — termos exatos (artigo de lei, súmula) e semântica se complementam.

## 7. Análise de caso (RAG + Claude)

Evolução direta do pipeline de 4 estágios atual, com o banco no meio:

```
peça do caso ──▶ 1. PERFIL (Opus, 1×, cache por hash — igual a hoje)
                     │  teses, fatos, precedente ideal, termos de busca
                     ▼
                2. RETRIEVAL (novo — substitui "ler o corpus inteiro")
                     │  busca híbrida por tese: termos do perfil + embedding
                     │  do resumo de cada tese → top-K acórdãos candidatos
                     │  (K configurável, ex. 40) + filtros do usuário
                     ▼
                3. EXTRAÇÃO (Sonnet, só nos candidatos SEM extração cacheada
                     │  em `extracoes`; em lote via Batches API)
                     ▼
                4. APLICAÇÃO (Opus, só nos relevantes — igual a hoje)
                     ▼
                5. SÍNTESE (Opus, 1×) ──▶ relatorio.md + .docx + citações
                                           clicáveis para o acórdão no acervo
```

Ganhos sobre o v1: custo por análise cai de O(corpus) para O(K); extrações são
**compartilhadas entre casos** (cache global em `extracoes`); análise roda como
job com progresso na UI (estágio, acórdão atual, custo acumulado em USD — o
contador de custo já existe no código); histórico de análises por caso.

Segurança mantida: `ANTHROPIC_API_KEY` só via ambiente; peças de caso nunca
versionadas; controle de acesso por usuário aos casos.

## 8. API (contrato resumido)

```
POST /api/auth/login
GET  /api/acordaos                 # busca facetada/híbrida (item 6)
GET  /api/acordaos/{id}            # metadados + texto + triagem + extração
GET  /api/acordaos/{id}/arquivo    # PDF/RTF original
GET  /api/facetas                  # contagens p/ filtros da UI
GET/POST/PATCH /api/temas          # temas de pesquisa (coleta)
POST /api/coletas                  # dispara coleta; GET /api/coletas/{id} status+log
POST /api/casos                    # upload da peça
POST /api/casos/{id}/analises      # dispara análise; GET .../analises/{id} progresso
GET  /api/analises/{id}/relatorio  # md/docx
GET/POST /api/criterios-triagem    # dicionários de triagem editáveis
```

## 9. Frontend (páginas)

1. **Dashboard** — acervo por câmara/relator/área, últimas coletas, alertas de
   precedentes contrários novos (a seção ALERTA da triagem vira card vivo).
2. **Pesquisa** — barra de busca (léxica/semântica/híbrida) + painel de filtros
   facetados com contagens + resultados com snippet destacado e badges
   (favorável/contrária, área, resultado). Salvar pesquisas.
3. **Acórdão** — viewer do inteiro teor, metadados, triagem com trechos que
   dispararam, extração LLM (ratio, passagens citáveis com botão copiar),
   acórdãos semelhantes (k-NN).
4. **Análise de caso** — upload da peça → revisão do perfil gerado (teses
   editáveis antes de gastar tokens) → acompanhamento do job em tempo real →
   relatório navegável por tese com export .docx.
5. **Coletas** — CRUD de temas de pesquisa, agenda, execução manual, log e stats
   por execução, diagnóstico de grafia de relator (o `--diagnostico` atual vira botão).
6. **Configurações** — critérios de triagem (dicionários/pesos editáveis),
   usuários, chaves e modelos (pesado/leve) usados.

## 10. Fases de execução

| Fase | Entrega | Conteúdo | Estimativa* |
|---|---|---|---|
| **0. Fundação** | repo reorganizado roda como hoje | monorepo, docker-compose (Postgres+pgvector), FastAPI esqueleto, Alembic, CI (testes atuais passando), scripts v1 em `legacy/` funcionando | 3–5 d |
| **1. Banco + ingestão** | acervo no Postgres | schema (item 3), scraper adaptado gravando no banco, extração de texto na ingestão, script de migração do `index.csv`, coletas como jobs com log | 1–1,5 sem |
| **2. Enriquecimento** | acervo classificado | taxonomia CNJ carregada, classificador LLM em lote (structured outputs + Batches), triagem versionada no banco, embeddings + pgvector | 1–1,5 sem |
| **3. API de busca** | backend consultável | endpoints de busca facetada, full-text, semântica e híbrida; facetas; auth básica | 1 sem |
| **4. Análise de caso** | RAG operacional | pipeline 5 estágios sobre o banco, jobs com progresso e custo, cache global de extrações, relatório md/docx | 1–1,5 sem |
| **5. Frontend** | plataforma usável | as 6 páginas do item 9 contra a API | 2–3 sem |
| **6. Operação** | pronto p/ uso contínuo | agendamento de coletas, backups (pg_dump + storage), observabilidade (métricas de custo LLM, falhas de coleta), multiusuário, hardening | 1 sem |

\* estimativas para desenvolvimento assistido por IA; fases 3–5 podem se sobrepor.

Critério de aceite de cada fase: testes automatizados (a suíte offline atual
migra e cresce — portal falso continua sendo o alicerce) + os fluxos v1
equivalentes funcionando na v2 antes de aposentar o script legado.

## 11. Riscos e cuidados

- **Portal do TJSC** é a dependência mais frágil (HTML sem contrato). Mitigação:
  snapshots `_raw_html` por coleta + testes de parser contra fixtures reais +
  alerta na UI quando uma coleta retorna 0 resultados onde havia histórico.
- **Uso responsável**: manter rate-limit ≥2s, sem paralelismo por host, robots.txt,
  User-Agent identificado — agendamentos devem ser espaçados (ex. 1×/dia por tema).
- **Custo de LLM**: teto de gasto por análise configurável (o contador já existe),
  Batches API para tudo que for lote, `--limite`/amostragem preservados na UI.
- **LGPD/sigilo**: acórdãos são públicos, mas as **peças do caso** não — storage
  segregado, acesso por dono, exclusão definitiva, nunca em log/commit.
- **Classificação automática erra**: origem (`regra|llm|manual`) gravada por
  rótulo, UI permite corrigir (vira `manual`, que prevalece) — o aviso da triagem
  ("prioriza leitura, não substitui análise jurídica") permanece em destaque.

## 12. Fora de escopo (por ora, registrado para depois)

- Outros tribunais (TJSP, STJ) — a arquitetura já suporta; falta só o scraper.
- Notificações (e-mail quando surgir precedente contrário em tema monitorado).
- Geração de minuta a partir do relatório (estágio 6 natural do pipeline).
- Chat com o acervo ("quais acórdãos da 10ª Câmara afastaram faturamento como parâmetro?") — trivial de adicionar depois que o RAG da Fase 4 existir.
