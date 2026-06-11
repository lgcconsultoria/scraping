#!/usr/bin/env python3
"""Raspador de jurisprudência do TJSC (https://busca.tjsc.jus.br/jurisprudencia/).

Coleta acórdãos de 8 desembargadores das 9ª e 10ª Câmaras de Direito Civil em
dois eixos temáticos (dicionário EIXOS, configurável abaixo), baixa o inteiro
teor de cada decisão (PDF ou RTF — o eproc entrega RTF —, com fallback para
HTML) e mantém um catálogo incremental em <saida>/index.csv.

Uso:
    python scraper_tjsc.py --dry-run    # busca e cataloga, sem baixar nada
    python scraper_tjsc.py              # busca e baixa PDFs/HTMLs
    python scraper_tjsc.py --out DIR    # muda o diretório de saída

Estrutura de saída:
    decisoes_tjsc/
      9a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
      10a_camara/<relator_slug>/<NUMERO_PROCESSO>.pdf
      index.csv                      # catálogo (uma linha por acórdão único)
      _raw_html/<relator>_<eixo>_p<N>.html   # snapshots para auditoria
      run.log

Boas práticas embutidas:
  - checa robots.txt antes de operar (aborta se o acesso for desautorizado);
  - User-Agent identificável com contato;
  - intervalo mínimo de 2 s entre TODAS as requisições (rate limiting global);
  - backoff exponencial em HTTP 429/5xx e erros de rede;
  - idempotência: arquivo já existente em disco não é rebaixado; o index.csv
    é gravado em append (retomável) e reconciliado ao final quando linhas
    antigas mudam de estado (ex.: download feito após um --dry-run);
  - try/except por documento: a falha de um PDF não aborta o lote (fica no
    run.log e é retentada na próxima execução).

Aviso: confira os Termos de Uso do portal antes de operar em escala. Este
script não paraleliza requisições.

Variáveis de ambiente (úteis em testes): TJSC_BASE (base alternativa do
portal) e TJSC_MIN_INTERVALO (segundos entre requisições; padrão 2).
"""

import argparse
import collections
import csv
import logging
import os
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Este script requer a biblioteca 'requests' (pip install requests).")

from nucleo.cache import salvar_texto
from nucleo.catalogo import (COLUNAS, slug,
                              carregar_index, regravar_index,
                              reservar_base, arquivo_existente)
from tribunais.tjsc import parse_resultados, tipo_do_documento, AdaptadorTjsc  # noqa: F401

BASE = os.environ.get("TJSC_BASE", "https://busca.tjsc.jus.br/jurisprudencia").rstrip("/")
OUT_PADRAO = "decisoes_tjsc"

CONTATO = "lgclicitacao@gmail.com"
USER_AGENT = (
    "LGCPesquisaJuridica/1.0 (coleta de jurisprudencia publica para pesquisa "
    f"juridica; rate limit >= 2s; contato: {CONTATO})"
)
MIN_INTERVALO = float(os.environ.get("TJSC_MIN_INTERVALO", "2"))
MAX_TENTATIVAS = 5
PS = 50
MAX_PAGINAS = 400

RELATORES = {
    "9a_camara": [
        "Cláudia Lambert de Faria",
        "Gerson Cherem II",
        "Haidée Denise Grin",
        "Flavio Andre Paz de Brum",
    ],
    "10a_camara": [
        "Silvio Dagoberto Orsatto",
        "Jaber Farah",
        "Sérgio Luiz Junkes",
        "Marcelo Pons Meirelles",
    ],
}

EIXOS = {
    "prova_cognicao": {
        "frase": "tutela de urgência",
        "q": "alimentos prova",
        "classe": "Agravo de Instrumento",
    },
    "binomio_renda": {
        "q": "alimentos possibilidade renda",
        "classe": "Agravo de Instrumento",
    },
    "alimentos_amplo": {
        "q": "alimentos",
        "datainicial": "01/01/2025",
    },
    "alimentos_ex_conjuge": {
        "q": "alimentos",
        "frase": "ex-cônjuge",
    },
}


def configurar_log(saida):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(saida, "run.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Raspa acórdãos do portal de jurisprudência do TJSC "
                    "(8 relatores das 9ª/10ª Câmaras de Direito Civil, 2 eixos temáticos).")
    parser.add_argument("--dry-run", action="store_true",
                        help="faz as buscas e monta o index.csv sem baixar inteiros teores")
    parser.add_argument("--out", default=OUT_PADRAO,
                        help=f"diretório de saída (padrão: {OUT_PADRAO})")
    parser.add_argument("--diagnostico", metavar="RELATOR",
                        help="roda UMA busca só por relator (sem tema/classe) e mostra o "
                             "total e os primeiros resultados; útil para validar grafias")
    parser.add_argument("--eixo", action="append", metavar="NOME",
                        help="limita a execução aos eixos informados (repita a flag); "
                             "opções: " + ", ".join(sorted(EIXOS)))
    args = parser.parse_args(argv)
    for nome_eixo in args.eixo or []:
        if nome_eixo not in EIXOS:
            parser.error(f"eixo desconhecido: {nome_eixo!r} (opções: {', '.join(sorted(EIXOS))})")

    saida = args.out
    os.makedirs(os.path.join(saida, "_raw_html"), exist_ok=True)
    configurar_log(saida)
    print(f"Aviso: respeite os Termos de Uso do portal TJSC. Este script lê o robots.txt, "
          f"identifica-se como '{USER_AGENT}' e aplica intervalo mínimo de "
          f"{MIN_INTERVALO:g}s entre requisições, sem paralelismo.")
    logging.info("início | dry_run=%s | saida=%s | base=%s | intervalo>=%ss",
                 args.dry_run, saida, BASE, MIN_INTERVALO)

    adaptador = AdaptadorTjsc(
        base=BASE,
        user_agent=USER_AGENT,
        min_intervalo=MIN_INTERVALO,
        max_tentativas=MAX_TENTATIVAS,
        ps=PS,
        max_paginas=MAX_PAGINAS,
    )
    try:
        if not adaptador.verificar_robots():
            logging.error("Abortando em respeito ao robots.txt do portal.")
            sys.exit(2)
        adaptador.estabelecer_sessao()
        logging.info("sessão estabelecida em %s/", BASE)
    except SystemExit:
        raise
    except requests.RequestException as exc:
        logging.error("não foi possível acessar %s: %s", BASE, exc)
        sys.exit(1)

    if args.diagnostico:
        relator = args.diagnostico
        for registros, total, _pagina, bruto in adaptador.buscar(relator, {}, ps=10):
            caminho_raw = os.path.join(saida, "_raw_html", f"diagnostico_{slug(relator)}.html")
            salvar_texto(caminho_raw, bruto)
            print(f"\nDiagnóstico | relator='{relator}' | {total} resultado(s) no portal "
                  "(busca só por relator, sem tema/classe)")
            for reg in registros[:10]:
                print(f"  {reg['numero_processo'] or '?':<28} | {reg['data_julgamento']:<10} | "
                      f"relator no portal: {reg['relator']!r} | {reg['orgao_julgador']}")
            if not registros:
                print("  Nenhum registro: a grafia provavelmente não bate com o índice do portal.")
            print(f"  Snapshot salvo em: {caminho_raw}")
            break
        return

    caminho_csv = os.path.join(saida, "index.csv")
    linhas, por_id = carregar_index(caminho_csv)
    if linhas:
        logging.info("catálogo existente carregado: %s linhas.", len(linhas))
    ocupados = {l["arquivo"].rsplit(".", 1)[0]: l["doc_id"] for l in linhas if l["arquivo"]}
    stats = collections.Counter()
    precisa_regravar = False
    csv_novo = not os.path.exists(caminho_csv) or os.path.getsize(caminho_csv) == 0
    arq_csv = open(caminho_csv, "a", newline="", encoding="utf-8")
    escritor = csv.DictWriter(arq_csv, fieldnames=COLUNAS)
    if csv_novo:
        escritor.writeheader()
        arq_csv.flush()

    def baixar(linha, base_rel):
        nonlocal precisa_regravar
        os.makedirs(os.path.join(saida, os.path.dirname(base_rel)), exist_ok=True)
        try:
            teor = adaptador.baixar_inteiro_teor(
                linha["doc_id"], tipo_do_documento(linha),
                os.path.join(saida, base_rel))
        except Exception as exc:
            stats["falhas_download"] += 1
            logging.error("download %s (processo %s): %s",
                          linha["doc_id"], linha["numero_processo"], exc)
            return False
        linha["arquivo"] = os.path.relpath(teor.caminho, saida).replace(os.sep, "/")
        linha["formato"] = teor.formato
        linha["url_integra"] = teor.url
        stats["baixados_" + teor.formato] += 1
        return True

    def processar_registro(camara, relator, eixo, reg):
        nonlocal precisa_regravar
        doc_id = reg["doc_id"]
        if not doc_id:
            stats["sem_id"] += 1
            logging.warning("registro sem doc_id (processo=%r) em %s/%s",
                            reg["numero_processo"], relator, eixo)
            return 0
        existente = por_id.get(doc_id)
        if existente is None:
            linha = {coluna: "" for coluna in COLUNAS}
            linha.update({chave: valor for chave, valor in reg.items() if chave in COLUNAS})
            linha["relator"] = reg["relator"] or relator
            linha["camara"] = camara
            linha["eixo_origem"] = eixo
            linha["url_integra"] = adaptador.url_integra(
                doc_id, reg.get("tipo_doc") or "acordao_eproc")
            base_rel = reservar_base(camara, relator, doc_id, reg["numero_processo"], ocupados)
            arquivo, formato = arquivo_existente(saida, base_rel)
            if arquivo:
                linha["arquivo"], linha["formato"] = arquivo, formato
                stats["pulados"] += 1
            elif not args.dry_run:
                baixar(linha, base_rel)
            escritor.writerow(linha)
            arq_csv.flush()
            linhas.append(linha)
            por_id[doc_id] = linha
            stats["novos"] += 1
            return 1
        stats["reencontrados"] += 1
        if args.dry_run:
            return 0
        arquivo = existente["arquivo"]
        if arquivo and os.path.exists(os.path.join(saida, arquivo)):
            stats["pulados"] += 1
            return 0
        base_rel = (arquivo.rsplit(".", 1)[0] if arquivo else
                    reservar_base(existente["camara"] or camara, relator,
                                  doc_id, existente["numero_processo"], ocupados))
        achado, formato = arquivo_existente(saida, base_rel)
        if achado:
            existente["arquivo"], existente["formato"] = achado, formato
            stats["pulados"] += 1
            precisa_regravar = True
            return 0
        if baixar(existente, base_rel):
            precisa_regravar = True
        return 0

    try:
        for camara, nomes in RELATORES.items():
            for relator in nomes:
                for eixo, params in EIXOS.items():
                    if args.eixo and eixo not in args.eixo:
                        continue
                    stats["consultas"] += 1
                    novos_consulta = 0
                    total_consulta = 0
                    try:
                        for registros, total, pagina, bruto in adaptador.buscar(relator, params):
                            total_consulta = total or total_consulta
                            caminho_raw = os.path.join(
                                saida, "_raw_html", f"{slug(relator)}_{eixo}_p{pagina}.html")
                            salvar_texto(caminho_raw, bruto)
                            logging.info("%s | %s | página %s | total %s | %s registros",
                                         relator, eixo, pagina, total, len(registros))
                            for reg in registros:
                                novos_consulta += processar_registro(camara, relator, eixo, reg)
                        logging.info("consulta concluída: %s | %s | total=%s | novos=%s",
                                     relator, eixo, total_consulta, novos_consulta)
                    except Exception as exc:
                        stats["falhas_busca"] += 1
                        logging.error("busca %s/%s: %s", relator, eixo, exc)
    except KeyboardInterrupt:
        stats["interrompido"] = 1
        logging.warning("interrompido pelo usuário; salvando estado parcial.")
    finally:
        arq_csv.close()
        if precisa_regravar:
            regravar_index(caminho_csv, linhas)
            logging.info("index.csv regravado para refletir downloads/atualizações.")

    resumo = ("consultas={consultas} | novos={novos} | reencontrados={reencontrados} | "
              "baixados_pdf={baixados_pdf} | baixados_rtf={baixados_rtf} | "
              "baixados_html={baixados_html} | "
              "pulados_existentes={pulados} | falhas_download={falhas_download} | "
              "falhas_busca={falhas_busca} | sem_id={sem_id}").format_map(stats)
    logging.info("FIM | %s", resumo)
    print(f"\nConcluído{' (dry-run: nada foi baixado)' if args.dry_run else ''}. {resumo}")
    print(f"Catálogo: {caminho_csv} ({len(linhas)} documentos) | log: "
          f"{os.path.join(saida, 'run.log')}")
    if stats["interrompido"]:
        sys.exit(130)


if __name__ == "__main__":
    main()
