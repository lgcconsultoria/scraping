# Raspador de jurisprudência do TJSC

Script de linha de comando que consulta o portal de jurisprudência do
Tribunal de Justiça de Santa Catarina (`https://busca.tjsc.jus.br/jurisprudencia/`),
coleta acórdãos de 8 desembargadores das **9ª e 10ª Câmaras de Direito Civil**
(Família/Sucessões) em dois eixos temáticos sobre alimentos, baixa o inteiro
teor de cada decisão (PDF, com fallback para HTML) e mantém um catálogo
`index.csv`.

## Instalação

```bash
pip install -r requirements.txt   # apenas 'requests'
```

## Uso

```bash
python scraper_tjsc.py --dry-run   # 1º passo: busca e monta o index.csv SEM baixar nada
python scraper_tjsc.py             # 2º passo: baixa os inteiros teores (PDF/RTF/HTML)
python scraper_tjsc.py --out DIR   # muda o diretório de saída (padrão: decisoes_tjsc)
python scraper_tjsc.py --eixo alimentos_amplo      # roda só um eixo (repita a flag)
python scraper_tjsc.py --diagnostico "Nome Aqui"   # busca só por relator: valida grafia
```

Fluxo recomendado: rode primeiro com `--dry-run`, revise o `index.csv` e só
então rode sem a flag para baixar em massa. O script é **idempotente**:
reexecutar não rebaixa arquivos já existentes e não duplica linhas no
catálogo; execuções interrompidas podem ser retomadas.

## Saída

```
decisoes_tjsc/
  9a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
  10a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
  index.csv          # numero_processo, relator, camara, orgao_julgador, comarca,
                     # data_julgamento, classe, doc_id, eixo_origem, formato,
                     # arquivo, url_integra
  _raw_html/         # snapshot de cada página de resultado (auditoria)
  run.log
```

Acórdãos do eproc são entregues pelo portal como **RTF** (abre no Word) e
salvos como `.rtf` com `formato=rtf`; quando o `integra.do` entrega PDF, sai
`.pdf`. Documentos sem PDF/RTF localizável são salvos como `.html` e marcados
com `formato=html` no catálogo. Quando dois acórdãos compartilham o
mesmo número de processo (ex.: mérito + embargos), o segundo arquivo recebe um
sufixo com o final do `doc_id`.

## Configuração

Os alvos e as buscas ficam em dicionários no topo de `scraper_tjsc.py`:

- `RELATORES` — relatores por câmara (grafias validadas no portal; a busca é
  por `relator`, pois o filtro `orgaoJulgador` do portal ainda não lista a
  9ª/10ª Câmaras);
- `EIXOS` — parâmetros de cada busca (`q`, `frase`, `classe`, `datainicial`,
  `datafinal` etc.). Mantenha 2–4 termos por busca.

## Uso responsável

O script lê o `robots.txt` do portal antes de operar (aborta se as rotas
forem desautorizadas), identifica-se com User-Agent contendo contato, aplica
intervalo mínimo de **2 s entre todas as requisições** (sem paralelismo) e
faz backoff exponencial em HTTP 429/5xx. Confira também os Termos de Uso do
portal antes de operar em escala.

## Triagem (pós-processamento)

Depois de baixar os documentos, o `triagem_tjsc.py` lê o texto de cada arquivo
(`.pdf`, `.rtf` e `.html`), pontua termos da tese jurídica em dois eixos
(prova/cognição sumária e binômio/capacidade contributiva) e classifica cada
decisão como FAVORÁVEL / CONTRÁRIA / NEUTRA por eixo. É 100% offline e
sobrescreve seus dois artefatos a cada execução:

- `decisoes_tjsc/triagem.csv` — uma linha por documento, com scores e termos;
- `decisoes_tjsc/relatorio_triagem.md` — relatório de leitura priorizada, com
  a seção **ALERTA** (precedentes potencialmente contrários) no topo e
  snippets do texto original que dispararam cada classificação.

```bash
python triagem_tjsc.py              # processa decisoes_tjsc/
python triagem_tjsc.py --out DIR    # outra pasta de saída
python triagem_tjsc.py --ocr        # tenta OCR em PDFs escaneados
                                    # (requer pytesseract + pdf2image)
```

> AVISO: é classificação automática por palavra-chave — prioriza a leitura,
> não substitui a análise jurídica. Um acórdão pode citar um termo justamente
> para afastá-lo; a seção ALERTA existe para leitura prioritária, não descarte.

PDFs sem texto extraível são marcados `precisa_ocr` e listados no apêndice do
relatório — nunca classificados como neutros por omissão. Dicionários de
termos, pesos e limiares ficam no topo de `triagem_tjsc.py`; ajustar um peso
muda a classificação sem nenhuma outra alteração de código.

## Relatório do caso (API do Claude)

O `relatorio_caso_tjsc.py` cruza uma peça do SEU caso (ex.: o agravo de
instrumento, em md/txt) com os acórdãos baixados: o Claude avalia cada acórdão
como precedente para a defesa (favorável/contrário/neutro + aplicabilidade
0-10 + trechos citáveis, em JSON estruturado) e, ao final, redige um relatório
de precedentes organizado por tese, com parágrafos prontos para a minuta e
estratégias de distinguishing para os precedentes de risco.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."    # chave SÓ via variável de ambiente
python relatorio_caso_tjsc.py --caso minha_peca.md --limite 5   # teste barato
python relatorio_caso_tjsc.py --caso minha_peca.md              # análise completa
python relatorio_caso_tjsc.py --caso minha_peca.md --modelo claude-sonnet-4-6  # mais barato
```

Saídas: `decisoes_tjsc/analises_caso.jsonl` (cache retomável — reexecutar não
re-analisa nem re-cobra o que já foi feito) e `decisoes_tjsc/relatorio_caso.md`.
A peça do caso entra como contexto com prompt caching (custo ~10x menor nas
chamadas seguintes) e NÃO deve ser commitada no repositório.

> SEGURANÇA: nunca grave a chave de API em código, arquivo versionado ou chat.
> Use somente a variável de ambiente `ANTHROPIC_API_KEY`. Se uma chave vazar,
> revogue-a imediatamente em console.anthropic.com.

### Sequência completa do fluxo

```bash
pip install -r requirements.txt
python scraper_tjsc.py --dry-run    # 1. busca e cataloga (confira o index.csv)
python scraper_tjsc.py              # 2. baixa os inteiros teores
python triagem_tjsc.py              # 3. classifica e gera o relatório de triagem
python relatorio_caso_tjsc.py --caso minha_peca.md --limite 5  # 4. cruza com o caso
open decisoes_tjsc/relatorio_caso.md
```

## Testes

Suíte offline contra um portal falso local (não toca a internet):

```bash
python test_scraper_tjsc.py -v
python test_triagem_tjsc.py -v
```

Cobre: parsing dos rótulos do portal (com entidades HTML), paginação,
deduplicação entre eixos, dry-run, os três caminhos de download (PDF direto,
PDF localizado no visualizador HTML, fallback `.html`), detecção de PDF por
magic bytes, colisão de nomes, idempotência e abortagem por `robots.txt`.

Variáveis de ambiente usadas nos testes: `TJSC_BASE` (base alternativa do
portal) e `TJSC_MIN_INTERVALO` (segundos entre requisições; padrão 2).
