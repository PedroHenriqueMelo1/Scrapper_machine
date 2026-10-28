from datetime import datetime
import logging

from engine.modelos.TiposMotor import Faixa
from model.RepoProtocolo import ProtocolRepositorio

registro_log = logging.getLogger("motor_de_raspagem")


class Repositorio_Faixas(ProtocolRepositorio):
    def __init__(self, client):
        self.conexao = client

    def adicionar_faixa(self, preco_minimo: int, preco_maximo: int) -> int | None:
        if preco_minimo > preco_maximo:
            raise ValueError("preco_minimo não pode ser maior que preco_maximo")

        existente = self.conexao.executar_query(
            "SELECT id FROM faixas_preco WHERE preco_minimo = ? AND preco_maximo = ?",
            (preco_minimo, preco_maximo),
        ).fetchone()

        if existente:
            registro_log.info(
                "Faixa %s-%s já existe (id %s)",
                preco_minimo, preco_maximo, existente[0],
            )
            return existente[0]

        sobreposta = self.conexao.executar_query(
            "SELECT preco_minimo, preco_maximo FROM faixas_preco "
            "WHERE preco_minimo <= ? AND preco_maximo >= ? LIMIT 1",
            (preco_maximo, preco_minimo),
        ).fetchone()

        if sobreposta:
            registro_log.error(
                "Faixa %s-%s recusada: sobrepõe a faixa %s-%s",
                preco_minimo, preco_maximo, sobreposta[0], sobreposta[1],
            )
            return None

        agora = datetime.now().isoformat()
        cursor = self.conexao.executar_query(
            """
            INSERT INTO faixas_preco
            (preco_minimo, preco_maximo, segmento_valor, data_criacao, data_atualizacao)
            VALUES (?, ?, ?, ?, ?)
            """,
            (preco_minimo, preco_maximo, f"{preco_minimo}-{preco_maximo}", agora, agora),
        )

        registro_log.info(
            "Faixa %s-%s criada (id %s)",
            preco_minimo, preco_maximo, cursor.lastrowid,
        )
        return cursor.lastrowid

    def obter_proxima_faixa_aberta(self) -> Faixa | None:
        linha = self.conexao.executar_query(
            """
            SELECT id, preco_minimo, preco_maximo, numero_paginas
            FROM faixas_preco
            WHERE finalizado = 0
            ORDER BY preco_minimo
            LIMIT 1
            """
        ).fetchone()

        if linha is None:
            return None

        return {
            "id": linha[0],
            "preco_minimo": linha[1],
            "preco_maximo": linha[2],
            "numero_paginas": linha[3],
        }

    def atualizar_progresso_faixa(
        self,
        faixa_id: int,
        paginas_concluidas: int,
        novos: int,
        motivo: str | None,
        observacao: str | None,
    ) -> None:
        # Não faz commit aqui: a transação deve ser controlada pelo cliente/camada chamadora.
        self.conexao.executar_query(
            """
            UPDATE faixas_preco
            SET numero_paginas = ?,
                imoveis_novos = imoveis_novos + ?,
                finalizado = ?,
                motivo_finalizacao = ?,
                observacao = COALESCE(?, observacao),
                data_atualizacao = ?
            WHERE id = ?
            """,
            (
                paginas_concluidas,
                novos,
                1 if motivo else 0,
                motivo,
                observacao,
                datetime.now().isoformat(),
                faixa_id,
            ),
        )
