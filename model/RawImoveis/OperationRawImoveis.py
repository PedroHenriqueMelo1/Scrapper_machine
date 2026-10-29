import logging
from datetime import datetime

registro_log = logging.getLogger("motor_de_raspagem")


class Repositorio_OperationRawImoveis:
    def __init__(self, cliente_banco):
        self.cliente_banco = cliente_banco

    def criar_tabela(self):
        # 1. Tabela de controle: uma linha por imóvel da tabela imoveis (a fila do segundo motor).
        # finalizado = 1: terminou (com ou sem dados completos, o motivo explica). 0: ficou aberto.
        # Obs.: o SQLite só barra FK inválida se PRAGMA foreign_keys = ON estiver ligado
        with self.cliente_banco.conexao:
            self.cliente_banco.executar_query(
                """
                CREATE TABLE IF NOT EXISTS operation_raw_imoveis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imovel_id TEXT NOT NULL UNIQUE,
                    finalizado INTEGER NOT NULL DEFAULT 0,
                    motivo TEXT,
                    data_criacao TEXT,
                    data_atualizacao TEXT,
                    FOREIGN KEY (imovel_id) REFERENCES imoveis (id)
                )
                """
            )

    def registrar_resultado(self, imovel_id, finalizado, motivo):
        # 2. SEM commit de propósito: roda na mesma transação do imóvel raspado.
        # Se a operação já existe (retentativa), atualiza em vez de duplicar
        agora = datetime.now().isoformat()
        self.cliente_banco.executar_query(
            """
            INSERT INTO operation_raw_imoveis
            (imovel_id, finalizado, motivo, data_criacao, data_atualizacao)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (imovel_id) DO UPDATE SET
                finalizado = excluded.finalizado,
                motivo = excluded.motivo,
                data_atualizacao = excluded.data_atualizacao
            """,
            (imovel_id, 1 if finalizado else 0, motivo, agora, agora),
        )

    def obter_pendentes(self, rowid_inicial, limite):
        # 3. Pendente = imóvel sem operação ainda, ou com operação aberta (finalizado = 0).
        # Pagina pelo rowid: assim um imóvel com erro de rede não volta na mesma execução
        linhas = self.cliente_banco.executar_query(
            """
            SELECT i.rowid, i.id, i.url
            FROM imoveis i
            LEFT JOIN operation_raw_imoveis o ON o.imovel_id = i.id
            WHERE i.rowid > ?
              AND (o.id IS NULL OR o.finalizado = 0)
            ORDER BY i.rowid
            LIMIT ?
            """,
            (rowid_inicial, limite),
        ).fetchall()
        return linhas

    def obter_resumo_por_motivo(self):
        return self.cliente_banco.executar_query(
            """
            SELECT finalizado, COALESCE(motivo, '-'), COUNT(*)
            FROM operation_raw_imoveis
            GROUP BY finalizado, motivo
            ORDER BY COUNT(*) DESC
            """
        ).fetchall()