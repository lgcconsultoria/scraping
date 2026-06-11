"""Gerenciamento do catálogo index.csv: colunas, caminhos, carregamento e escrita."""
import csv
import os
import re
import unicodedata

COLUNAS = [
    "numero_processo", "relator", "camara", "orgao_julgador", "comarca",
    "data_julgamento", "classe", "doc_id", "eixo_origem", "formato",
    "arquivo", "url_integra",
]


def slug(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = re.sub(r"\s+", "_", texto.strip().lower())
    return re.sub(r"[^a-z0-9_]+", "", texto)


def sanitizar_nome(nome):
    return re.sub(r"[^\w.\-]+", "_", nome).strip("._-")[:150]


def carregar_index(caminho_csv):
    """Carrega o catálogo existente: (todas_as_linhas, dicionário doc_id -> linha)."""
    linhas, por_id = [], {}
    if not os.path.exists(caminho_csv):
        return linhas, por_id
    with open(caminho_csv, newline="", encoding="utf-8") as arq:
        for crua in csv.DictReader(arq):
            linha = {coluna: (crua.get(coluna) or "").strip() for coluna in COLUNAS}
            linhas.append(linha)
            if linha["doc_id"]:
                por_id.setdefault(linha["doc_id"], linha)
    return linhas, por_id


def regravar_index(caminho_csv, linhas):
    tmp = caminho_csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as arq:
        escritor = csv.DictWriter(arq, fieldnames=COLUNAS)
        escritor.writeheader()
        escritor.writerows(linhas)
    os.replace(tmp, caminho_csv)


def reservar_base(camara, relator_consulta, doc_id, numero, ocupados):
    """Caminho relativo (sem extensão) do arquivo, com sufixo em caso de colisão
    de nome (ex.: dois acórdãos do mesmo processo: mérito + embargos)."""
    nome = sanitizar_nome(numero) or sanitizar_nome(doc_id) or "documento"
    base_rel = f"{camara}/{slug(relator_consulta)}/{nome}"
    dono = ocupados.get(base_rel)
    if dono and dono != doc_id:
        base_rel = f"{base_rel}_{sanitizar_nome(doc_id[-8:]) or 'dup'}"
    ocupados[base_rel] = doc_id
    return base_rel


def arquivo_existente(saida, base_rel):
    for extensao, formato in ((".pdf", "pdf"), (".rtf", "rtf"), (".html", "html")):
        if os.path.exists(os.path.join(saida, base_rel + extensao)):
            return base_rel + extensao, formato
    return "", ""
