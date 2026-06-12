"""Repositório híbrido de decisões judiciais: SQLite + arquivos em disco.

Metadados e índice FTS5 ficam no SQLite; binários (PDF/RTF) ficam no
disco referenciados pela tabela downloads.

Uso típico:
    from acervo.db import Acervo

    with Acervo("acervo.db") as db:
        db.inserir(decisao_dict)
        resultados = db.buscar_texto("alimentos gravídicos")
"""
import json
import os
import sqlite3
from datetime import datetime, timezone


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Acervo:
    """Acervo de decisões judiciais com FTS5 e rastreamento de downloads."""

    def __init__(self, caminho_db: str = "acervo.db"):
        os.makedirs(os.path.dirname(caminho_db) or ".", exist_ok=True)
        self.caminho = caminho_db
        self._con = sqlite3.connect(caminho_db, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._inicializar()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "Acervo":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Inicialização do schema
    # ------------------------------------------------------------------

    def _inicializar(self) -> None:
        c = self._con
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")

        c.execute("""
            CREATE TABLE IF NOT EXISTS decisoes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id          TEXT    NOT NULL UNIQUE,
                tribunal        TEXT    NOT NULL,
                relator         TEXT,
                tipo            TEXT,
                data_julgamento TEXT,
                ementa          TEXT,
                url             TEXT,
                criado_em       TEXT    NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id       TEXT    NOT NULL REFERENCES decisoes(doc_id) ON DELETE CASCADE,
                caminho      TEXT    NOT NULL UNIQUE,
                formato      TEXT    NOT NULL,
                baixado_em   TEXT    NOT NULL,
                hash_arquivo TEXT
            )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_dec_tribunal ON decisoes(tribunal)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dec_relator  ON decisoes(relator)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dec_data     ON decisoes(data_julgamento)")

        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_decisoes USING fts5(
                doc_id UNINDEXED,
                ementa,
                content='decisoes',
                content_rowid='id'
            )
        """)

        c.execute("""
            CREATE TRIGGER IF NOT EXISTS decisoes_ai AFTER INSERT ON decisoes BEGIN
                INSERT INTO fts_decisoes(rowid, doc_id, ementa)
                VALUES (new.id, new.doc_id, new.ementa);
            END
        """)

        c.execute("""
            CREATE TRIGGER IF NOT EXISTS decisoes_ad AFTER DELETE ON decisoes BEGIN
                INSERT INTO fts_decisoes(fts_decisoes, rowid, doc_id, ementa)
                VALUES ('delete', old.id, old.doc_id, old.ementa);
            END
        """)

        c.execute("""
            CREATE TRIGGER IF NOT EXISTS decisoes_au AFTER UPDATE ON decisoes BEGIN
                INSERT INTO fts_decisoes(fts_decisoes, rowid, doc_id, ementa)
                VALUES ('delete', old.id, old.doc_id, old.ementa);
                INSERT INTO fts_decisoes(rowid, doc_id, ementa)
                VALUES (new.id, new.doc_id, new.ementa);
            END
        """)

        c.commit()

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def inserir(self, decisao: dict) -> bool:
        """Insere uma decisão. Retorna True se nova, False se duplicata."""
        try:
            self._con.execute(
                """INSERT INTO decisoes
                   (doc_id, tribunal, relator, tipo, data_julgamento, ementa, url, criado_em)
                   VALUES (:doc_id, :tribunal, :relator, :tipo,
                           :data_julgamento, :ementa, :url, :criado_em)""",
                {
                    "doc_id":          decisao["doc_id"],
                    "tribunal":        decisao.get("tribunal", ""),
                    "relator":         decisao.get("relator", ""),
                    "tipo":            decisao.get("tipo", ""),
                    "data_julgamento": decisao.get("data_julgamento", ""),
                    "ementa":          decisao.get("ementa", ""),
                    "url":             decisao.get("url", ""),
                    "criado_em":       _agora(),
                },
            )
            self._con.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def registrar_download(
        self,
        doc_id: str,
        caminho: str,
        formato: str,
        hash_arquivo: str = "",
    ) -> None:
        """Registra (ou atualiza) o arquivo baixado para uma decisão."""
        self._con.execute(
            """INSERT OR REPLACE INTO downloads
               (doc_id, caminho, formato, baixado_em, hash_arquivo)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, caminho, formato, _agora(), hash_arquivo),
        )
        self._con.commit()

    def excluir(self, doc_id: str) -> bool:
        """Remove decisão e downloads associados. Retorna True se existia."""
        cur = self._con.execute("DELETE FROM decisoes WHERE doc_id = ?", (doc_id,))
        self._con.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def obter(self, doc_id: str) -> dict | None:
        """Retorna metadados da decisão ou None."""
        cur = self._con.execute("SELECT * FROM decisoes WHERE doc_id = ?", (doc_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def downloads(self, doc_id: str) -> list[dict]:
        """Lista arquivos baixados para a decisão."""
        cur = self._con.execute(
            "SELECT * FROM downloads WHERE doc_id = ? ORDER BY baixado_em", (doc_id,)
        )
        return [dict(r) for r in cur.fetchall()]

    def buscar_texto(self, consulta: str, limite: int = 50) -> list[dict]:
        """Busca FTS5 na ementa. Retorna lista de dicts."""
        cur = self._con.execute(
            """SELECT d.* FROM decisoes d
               WHERE d.id IN (
                   SELECT rowid FROM fts_decisoes WHERE fts_decisoes MATCH ?
               )
               LIMIT ?""",
            (consulta, limite),
        )
        return [dict(r) for r in cur.fetchall()]

    def buscar_filtros(
        self,
        *,
        tribunal: str = "",
        relator: str = "",
        data_inicio: str = "",
        data_fim: str = "",
        limite: int = 200,
    ) -> list[dict]:
        """Busca por campos indexados com filtros opcionais."""
        conds: list[str] = []
        params: list = []
        if tribunal:
            conds.append("tribunal = ?")
            params.append(tribunal)
        if relator:
            conds.append("relator LIKE ?")
            params.append(f"%{relator}%")
        if data_inicio:
            conds.append("data_julgamento >= ?")
            params.append(data_inicio)
        if data_fim:
            conds.append("data_julgamento <= ?")
            params.append(data_fim)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limite)
        cur = self._con.execute(
            f"SELECT * FROM decisoes {where} ORDER BY data_julgamento DESC LIMIT ?",
            params,
        )
        return [dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        """Estatísticas resumidas do acervo."""
        total = self._con.execute("SELECT COUNT(*) FROM decisoes").fetchone()[0]
        por_tribunal = dict(
            self._con.execute(
                "SELECT tribunal, COUNT(*) FROM decisoes GROUP BY tribunal ORDER BY 2 DESC"
            ).fetchall()
        )
        n_downloads = self._con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        return {"total": total, "por_tribunal": por_tribunal, "downloads": n_downloads}

    def exportar_jsonl(self, caminho: str, **filtros) -> int:
        """Exporta decisões para JSONL. Retorna número de registros exportados."""
        registros = (
            self.buscar_filtros(**filtros)
            if filtros
            else self.buscar_filtros(limite=999_999)
        )
        os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
        tmp = caminho + ".tmp"
        with open(tmp, "w", encoding="utf-8") as arq:
            for reg in registros:
                arq.write(json.dumps(reg, ensure_ascii=False) + "\n")
        os.replace(tmp, caminho)
        return len(registros)
