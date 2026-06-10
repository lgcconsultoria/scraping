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
python scraper_tjsc.py             # 2º passo: baixa os inteiros teores (PDF/HTML)
python scraper_tjsc.py --out DIR   # muda o diretório de saída (padrão: decisoes_tjsc)
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

## Testes

Suíte offline contra um portal falso local (não toca a internet):

```bash
python test_scraper_tjsc.py -v
```

Cobre: parsing dos rótulos do portal (com entidades HTML), paginação,
deduplicação entre eixos, dry-run, os três caminhos de download (PDF direto,
PDF localizado no visualizador HTML, fallback `.html`), detecção de PDF por
magic bytes, colisão de nomes, idempotência e abortagem por `robots.txt`.

Variáveis de ambiente usadas nos testes: `TJSC_BASE` (base alternativa do
portal) e `TJSC_MIN_INTERVALO` (segundos entre requisições; padrão 2).
