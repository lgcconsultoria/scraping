#!/usr/bin/env python3
"""Relatório de precedentes do caso concreto via API do Claude.

Cruza a peça processual do caso (ex.: o agravo de instrumento) com os
acórdãos do TJSC já baixados pelo scraper, usando o Claude para avaliar,
acórdão a acórdão, se a decisão serve de precedente PARA A DEFESA, com que
força, e quais trechos citar — e, ao final, redigir um relatório de
precedentes organizado por tese, com sugestões de parágrafo para a minuta.

Uso:
    export ANTHROPIC_API_KEY="sk-ant-..."   # nunca grave a chave em arquivo/código
    python relatorio_caso_tjsc.py --caso minha_peca.md --limite 5   # teste barato
    python relatorio_caso_tjsc.py --caso minha_peca.md              # análise completa

Saídas (na pasta --out):
    analises_caso.jsonl   - análise estruturada por acórdão (cache retomável:
                            reexecutar não re-analisa nem re-cobra o que já foi feito)
    relatorio_caso.md     - relatório final de precedentes

Arquitetura/custo: a peça do caso entra como bloco de sistema com prompt
caching (cobrada ~1x na primeira chamada e ~0,1x nas seguintes); cada acórdão
é analisado com saída estruturada (JSON garantido por schema); a síntese final
recebe as melhores análises. Modelo padrão: claude-opus-4-8 (mude com
--modelo claude-sonnet-4-6 para reduzir custo). O custo estimado é impresso
ao final com base no uso real de tokens.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys

try:
    import anthropic
except ImportError:  # pragma: no cover
    sys.exit("Este script requer o SDK da Anthropic: pip install anthropic")

import triagem_tjsc  # reutiliza a extração de texto (.pdf/.rtf/.html)

OUT_PADRAO = "decisoes_tjsc"
MODELO_PADRAO = os.environ.get("TJSC_MODELO", "claude-opus-4-8")
MAX_TOKENS_ANALISE = 16000
MAX_TOKENS_RELATORIO = 16000
MIN_CHARS_TEXTO = 200       # menos que isso: documento sem texto útil
TETO_CHARS_ACORDAO = 45000  # ~12k tokens por acórdão (mantém início e fim)
TOP_FAVORAVEIS = 25         # quantas análises favoráveis entram na síntese

# Preço por milhão de tokens (entrada, saída) — cache: escrita 1,25x, leitura 0,1x.
PRECOS_USD = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

ESQUEMA_ANALISE = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "posicao": {
                "type": "string",
                "enum": ["favoravel", "contrario", "neutro"],
                "description": "favoravel: a ratio decidendi apoia tese da defesa do "
                               "Agravante; contrario: fortalece a parte adversa; "
                               "neutro: irrelevante ou inconclusivo para o caso.",
            },
            "aplicabilidade": {
                "type": "integer",
                "enum": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
                "description": "0-10: identidade fática e jurídica com o caso "
                               "(10 = precedente praticamente idêntico).",
            },
            "teses_apoiadas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Teses do agravo que o acórdão apoia (ou ameaça, se "
                               "contrário), em frases curtas.",
            },
            "resumo_relevancia": {
                "type": "string",
                "description": "2-4 frases: o que o acórdão decidiu e por que importa "
                               "(ou não) para este caso.",
            },
            "trechos_citaveis": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "trecho": {"type": "string",
                                   "description": "Citação LITERAL do acórdão."},
                        "como_usar": {"type": "string",
                                      "description": "Onde/como usar na defesa."},
                    },
                    "required": ["trecho", "como_usar"],
                    "additionalProperties": False,
                },
            },
            "ressalvas": {
                "type": "string",
                "description": "Riscos de distinguishing, contexto fático diverso, ou "
                               "string vazia se não houver.",
            },
        },
        "required": ["posicao", "aplicabilidade", "teses_apoiadas",
                     "resumo_relevancia", "trechos_citaveis", "ressalvas"],
        "additionalProperties": False,
    },
}

SISTEMA_MODELO = """Você é um assistente jurídico especializado em direito de família \
brasileiro e em jurisprudência do TJSC, auxiliando o advogado do AGRAVANTE no caso \
abaixo. Sua tarefa é avaliar acórdãos do TJSC, um por vez, como possíveis precedentes \
para a defesa.

Critérios:
- "favoravel" exige que a ratio decidendi (não um obiter dictum) apoie tese concreta \
da defesa; um acórdão que nega o que o Agravante também pede é "contrario".
- A aplicabilidade (0-10) pondera identidade fática: alimentos entre ex-cônjuges (sem \
filhos), tutela provisória/cognição sumária, alimentante sócio de empresa, faturamento \
bruto vs. pró-labore/renda efetiva, capacidade laborativa do credor, conduta indigna \
(art. 1.708 do CC), empresa como terceiro não sujeito passivo.
- "trechos_citaveis" devem ser citações LITERAIS do texto do acórdão fornecido; nunca \
invente ou parafraseie dentro do campo "trecho".
- Seja cético: na dúvida entre favoravel e neutro, prefira neutro e explique em \
"ressalvas".

=== PEÇA DO CASO (íntegra) ===
{caso}
=== FIM DA PEÇA DO CASO ==="""

INSTRUCAO_RELATORIO = """Com base na peça do caso (no contexto de sistema) e nas \
análises estruturadas abaixo (uma por acórdão do TJSC), redija um RELATÓRIO DE \
PRECEDENTES em markdown, em português, para uso do advogado do Agravante, contendo:

1. **Sumário executivo** (3-6 parágrafos): força da jurisprudência local para cada \
tese central do agravo, indicando onde a defesa está bem amparada e onde está exposta.
2. **Precedentes favoráveis, organizados por tese**, do mais forte ao mais fraco, cada \
um com: identificação completa, por que se aplica, o trecho citável mais forte, e um \
parágrafo PRONTO PARA A MINUTA citando o precedente.
3. **Precedentes contrários ou de risco**, cada um com estratégia objetiva de \
distinguishing em relação ao caso concreto.
4. **Lacunas**: teses do agravo sem respaldo na amostra analisada e o que buscar.

Cite sempre no formato: (TJSC, <classe> n. <número>, rel. <relator>, <órgão julgador>, \
j. <data>). Use apenas os acórdãos das análises abaixo; não invente precedentes.

=== ANÁLISES ===
{analises}"""


def recortar(texto, teto=TETO_CHARS_ACORDAO):
    """Compacta espaços e, se necessário, corta o MIOLO do acórdão (ementa fica
    no início e dispositivo no fim; o relatório intermediário é o sacrificável)."""
    texto = re.sub(r"[ \t]+", " ", texto).strip()
    if len(texto) <= teto:
        return texto
    inicio = int(teto * 0.75)
    fim = teto - inicio
    return (texto[:inicio] + "\n[... trecho intermediário omitido por limite de tamanho ...]\n"
            + texto[-fim:])


def carregar_indice(saida):
    caminho = os.path.join(saida, "index.csv")
    if not os.path.exists(caminho):
        sys.exit(f"{caminho} não encontrado — rode antes o scraper_tjsc.py (com download).")
    with open(caminho, newline="", encoding="utf-8") as arq:
        linhas = [l for l in csv.DictReader(arq) if l.get("doc_id")]
    # Com triagem disponível, analisa primeiro os mais promissores (útil com --limite)
    caminho_triagem = os.path.join(saida, "triagem.csv")
    if os.path.exists(caminho_triagem):
        with open(caminho_triagem, newline="", encoding="utf-8") as arq:
            notas = {}
            for linha in csv.DictReader(arq):
                try:
                    notas[linha["arquivo"]] = max(int(linha["score_eixo1"] or 0),
                                                  int(linha["score_eixo2"] or 0))
                except ValueError:
                    pass
        linhas.sort(key=lambda l: notas.get(l.get("arquivo", ""), -99), reverse=True)
    return linhas


def carregar_analises(caminho_jsonl):
    analises = {}
    if os.path.exists(caminho_jsonl):
        with open(caminho_jsonl, encoding="utf-8") as arq:
            for linha in arq:
                linha = linha.strip()
                if linha:
                    registro = json.loads(linha)
                    analises[registro["doc_id"]] = registro
    return analises


def texto_da_resposta(resposta):
    return "".join(bloco.text for bloco in resposta.content if bloco.type == "text")


def somar_uso(total, uso):
    total["entrada"] += uso.input_tokens
    total["saida"] += uso.output_tokens
    total["cache_escrita"] += getattr(uso, "cache_creation_input_tokens", 0) or 0
    total["cache_leitura"] += getattr(uso, "cache_read_input_tokens", 0) or 0


def custo_usd(total, modelo):
    preco_in, preco_out = PRECOS_USD.get(modelo, (5.0, 25.0))
    return (total["entrada"] * preco_in + total["cache_escrita"] * preco_in * 1.25
            + total["cache_leitura"] * preco_in * 0.1
            + total["saida"] * preco_out) / 1_000_000


def gerar_relatorio_md(caminho, texto_sintese, analises, modelo):
    import datetime
    ordenadas = sorted(analises.values(),
                       key=lambda r: (r["analise"]["posicao"] != "favoravel",
                                      -r["analise"]["aplicabilidade"]))
    linhas = [
        "# Relatório de Precedentes — caso concreto × acórdãos TJSC",
        "> AVISO: relatório gerado por IA (modelo "
        f"{modelo}) a partir dos acórdãos baixados. Ferramenta de apoio — confira cada "
        "precedente no inteiro teor antes de citar em juízo.",
        "",
        f"Gerado em: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')} | "
        f"Acórdãos analisados: {len(analises)}",
        "",
        texto_sintese.strip(),
        "",
        "---",
        "",
        "## Apêndice — todas as análises (ordenadas por utilidade)",
        "",
        "| Processo | Posição | Aplic. | Relator | Data | Resumo |",
        "|---|---|---|---|---|---|",
    ]
    for registro in ordenadas:
        analise = registro["analise"]
        resumo = analise["resumo_relevancia"].replace("|", "/").replace("\n", " ")
        linhas.append(f"| {registro['numero_processo']} | {analise['posicao']} | "
                      f"{analise['aplicabilidade']}/10 | {registro['relator']} | "
                      f"{registro['data_julgamento']} | {resumo} |")
    linhas.append("")
    with open(caminho, "w", encoding="utf-8") as arq:
        arq.write("\n".join(linhas))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Avalia os acórdãos baixados como precedentes para um caso concreto "
                    "(API do Claude) e gera relatorio_caso.md.")
    parser.add_argument("--caso", required=True,
                        help="arquivo com a peça do caso (md/txt) — fica fora do repositório")
    parser.add_argument("--out", default=OUT_PADRAO,
                        help=f"pasta de saída do scraper (padrão: {OUT_PADRAO})")
    parser.add_argument("--limite", type=int, default=0,
                        help="analisa no máximo N acórdãos novos (0 = todos); comece com 5")
    parser.add_argument("--modelo", default=MODELO_PADRAO,
                        help=f"modelo da API (padrão: {MODELO_PADRAO})")
    parser.add_argument("--refazer", action="store_true",
                        help="ignora o cache e re-analisa todos os acórdãos (re-cobra!)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Defina a variável de ambiente ANTHROPIC_API_KEY antes de rodar "
                 '(ex.: export ANTHROPIC_API_KEY="sk-ant-...").')

    with open(args.caso, encoding="utf-8") as arq:
        texto_caso = arq.read()
    sistema = [{
        "type": "text",
        "text": SISTEMA_MODELO.format(caso=texto_caso),
        "cache_control": {"type": "ephemeral"},  # peça do caso paga ~1x e reusa a ~0,1x
    }]
    logging.info("peça do caso: %s (~%s mil caracteres) | modelo: %s",
                 args.caso, len(texto_caso) // 1000, args.modelo)

    caminho_jsonl = os.path.join(args.out, "analises_caso.jsonl")
    if args.refazer and os.path.exists(caminho_jsonl):
        os.remove(caminho_jsonl)
    analises = carregar_analises(caminho_jsonl)
    documentos = carregar_indice(args.out)
    pendentes = [l for l in documentos if l["doc_id"] not in analises and l.get("arquivo")]
    if args.limite > 0:
        pendentes = pendentes[:args.limite]
    logging.info("documentos no catálogo: %s | já analisados: %s | a analisar agora: %s",
                 len(documentos), len(analises), len(pendentes))

    cliente = anthropic.Anthropic(max_retries=4)
    uso_total = {"entrada": 0, "saida": 0, "cache_escrita": 0, "cache_leitura": 0}
    falhas = 0
    interrompido = False
    with open(caminho_jsonl, "a", encoding="utf-8") as arq_jsonl:
        for indice, linha in enumerate(pendentes, 1):
            rotulo = linha["numero_processo"] or linha["doc_id"]
            try:
                caminho_doc = os.path.join(args.out, linha["arquivo"])
                if not os.path.exists(caminho_doc):
                    logging.warning("[%s/%s] %s: arquivo ausente, pulando (rode o "
                                    "scraper sem --dry-run)", indice, len(pendentes), rotulo)
                    continue
                texto = triagem_tjsc.extrair_texto(caminho_doc)
                if len(texto.strip()) < MIN_CHARS_TEXTO:
                    logging.warning("[%s/%s] %s: sem texto extraível, pulando",
                                    indice, len(pendentes), rotulo)
                    continue
                pergunta = (
                    "Analise o acórdão abaixo como possível precedente para a defesa "
                    "do Agravante no caso do contexto de sistema.\n\n"
                    f"Processo: {linha['numero_processo']} | Relator: {linha['relator']} | "
                    f"Órgão julgador: {linha['orgao_julgador']} | "
                    f"Julgado em: {linha['data_julgamento']} | Classe: {linha['classe']}\n\n"
                    f"=== INTEIRO TEOR ===\n{recortar(texto)}")
                resposta = cliente.messages.create(
                    model=args.modelo,
                    max_tokens=MAX_TOKENS_ANALISE,
                    thinking={"type": "adaptive"},
                    system=sistema,
                    messages=[{"role": "user", "content": pergunta}],
                    output_config={"format": ESQUEMA_ANALISE},
                )
                analise = json.loads(texto_da_resposta(resposta))
                somar_uso(uso_total, resposta.usage)
                registro = {chave: linha.get(chave, "") for chave in
                            ("doc_id", "numero_processo", "relator", "camara",
                             "orgao_julgador", "data_julgamento", "classe", "arquivo")}
                registro["analise"] = analise
                arq_jsonl.write(json.dumps(registro, ensure_ascii=False) + "\n")
                arq_jsonl.flush()
                analises[linha["doc_id"]] = registro
                logging.info("[%s/%s] %s -> %s (%s/10) | custo acumulado ~US$ %.2f",
                             indice, len(pendentes), rotulo, analise["posicao"],
                             analise["aplicabilidade"], custo_usd(uso_total, args.modelo))
            except anthropic.AuthenticationError:
                sys.exit("Chave de API inválida/revogada (401). Gere uma nova em "
                         "console.anthropic.com e exporte ANTHROPIC_API_KEY.")
            except KeyboardInterrupt:
                logging.warning("interrompido; o que já foi analisado está salvo em %s",
                                caminho_jsonl)
                interrompido = True
                break
            except Exception as exc:
                falhas += 1
                logging.error("[%s/%s] %s: %s", indice, len(pendentes), rotulo, exc)

    if not analises:
        sys.exit("Nenhuma análise disponível — nada para sintetizar.")

    favoraveis = sorted((r for r in analises.values()
                         if r["analise"]["posicao"] == "favoravel"),
                        key=lambda r: -r["analise"]["aplicabilidade"])[:TOP_FAVORAVEIS]
    contrarios = [r for r in analises.values() if r["analise"]["posicao"] == "contrario"]
    logging.info("síntese: %s favoráveis (top %s) e %s contrários de %s análises",
                 len(favoraveis), TOP_FAVORAVEIS, len(contrarios), len(analises))
    corpo = json.dumps({"favoraveis": favoraveis, "contrarios_ou_risco": contrarios},
                       ensure_ascii=False, indent=1)
    resposta = cliente.messages.create(
        model=args.modelo,
        max_tokens=MAX_TOKENS_RELATORIO,
        thinking={"type": "adaptive"},
        system=sistema,
        messages=[{"role": "user", "content": INSTRUCAO_RELATORIO.format(analises=corpo)}],
    )
    somar_uso(uso_total, resposta.usage)
    caminho_md = os.path.join(args.out, "relatorio_caso.md")
    gerar_relatorio_md(caminho_md, texto_da_resposta(resposta), analises, args.modelo)

    custo = custo_usd(uso_total, args.modelo)
    print(f"\nConcluído{' (parcial: interrompido)' if interrompido else ''}. "
          f"análises={len(analises)} | favoráveis={sum(1 for r in analises.values() if r['analise']['posicao'] == 'favoravel')} | "
          f"contrários={len(contrarios)} | falhas={falhas}")
    print(f"Tokens: entrada={uso_total['entrada']:,} | cache_escrita={uso_total['cache_escrita']:,} | "
          f"cache_leitura={uso_total['cache_leitura']:,} | saída={uso_total['saida']:,} "
          f"| custo estimado ~US$ {custo:.2f}")
    print(f"Relatório: {caminho_md}\nAnálises: {caminho_jsonl}")


if __name__ == "__main__":
    main()
