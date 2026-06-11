"""Cache em disco: hashes SHA-256, leitura e escrita atômica de JSON e JSONL."""
import hashlib
import json
import os


def sha256_texto(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def sha256_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as arq:
        for bloco in iter(lambda: arq.read(65536), b""):
            h.update(bloco)
    return h.hexdigest()


def carregar_jsonl(caminho):
    """dict doc_id -> registro (última ocorrência vence; tolera duplicatas)."""
    registros = {}
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as arq:
            for linha in arq:
                linha = linha.strip()
                if linha:
                    reg = json.loads(linha)
                    registros[reg["doc_id"]] = reg
    return registros


def compactar_jsonl(caminho, registros):
    """Reescreve o arquivo só com os registros vivos (remove duplicatas/obsoletos)."""
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as arq:
        for reg in registros.values():
            arq.write(json.dumps(reg, ensure_ascii=False) + "\n")
    os.replace(tmp, caminho)


def carregar_json(caminho):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8") as arq:
            return json.load(arq)
    return None


def salvar_json(caminho, dados):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as arq:
        json.dump(dados, arq, ensure_ascii=False, indent=1)
    os.replace(tmp, caminho)
