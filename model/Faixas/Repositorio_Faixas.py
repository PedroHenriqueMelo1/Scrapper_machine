import logging
from datetime import datetime

registro_log = logging.getLogger("motor_de_raspagem")


class Repositorio_Faixas:
    def __init__(self, cliente_banco):
        self.cliente_banco = cliente_banco

    def criar_tabela(self):
        # 1. Único lugar onde o schema de faixas_preco existe
        with self.cliente_banco.conexao:
            self.cliente_banco.executar_query(
                """
                CREATE TABLE IF NOT EXISTS faixas_preco (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preco_minimo INTEGER NOT NULL,
                    preco_maximo INTEGER NOT NULL,
                    segmento_valor TEXT,
                    numero_paginas INTEGER DEFAULT 0,
                    finalizado INTEGER DEFAULT 0,
                    motivo_finalizacao TEXT,
                    imoveis_novos INTEGER DEFAULT 0,
                    observacao TEXT,
                    data_criacao TEXT,
                    data_atualizacao TEXT,
                    UNIQUE (preco_minimo, preco_maximo)
                )
                """
            )

    def adicionar_faixa(self, preco_minimo, preco_maximo):
        # 2. Regra de negócio: mínimo não pode exceder o máximo
        if preco_minimo > preco_maximo:
            raise ValueError("preco_minimo não pode ser maior que preco_maximo")

        # 3. Faixa idêntica já existe: devolve o id dela
        existente = self.cliente_banco.executar_query(
            "SELECT id FROM faixas_preco WHERE preco_minimo = ? AND preco_maximo = ?",
            (preco_minimo, preco_maximo),
        ).fetchone()
        if existente:
            registro_log.info("Faixa %s-%s já existe (id %s)", preco_minimo, preco_maximo, existente[0])
            return existente[0]

        # 4. Faixa que sobrepõe outra duplicaria a coleta
        sobreposta = self.cliente_banco.executar_query(
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
        with self.cliente_banco.conexao:
            cursor = self.cliente_banco.executar_query(
                """
                INSERT INTO faixas_preco
                (preco_minimo, preco_maximo, segmento_valor, data_criacao, data_atualizacao)
                VALUES (?, ?, ?, ?, ?)
                """,
                (preco_minimo, preco_maximo, f"{preco_minimo}-{preco_maximo}", agora, agora),
            )
        registro_log.info("Faixa %s-%s criada (id %s)", preco_minimo, preco_maximo, cursor.lastrowid)
        return cursor.lastrowid

    def criar_proxima_faixa(self, passo_faixa_preco):
        # 5. Começa em "maior preço cadastrado + 1", sem buracos
        maior_preco_cadastrado = self.cliente_banco.executar_query(
            "SELECT MAX(preco_maximo) FROM faixas_preco"
        ).fetchone()[0]
        if maior_preco_cadastrado is None:
            return None

        self.adicionar_faixa(maior_preco_cadastrado + 1, maior_preco_cadastrado + passo_faixa_preco)
        return self.obter_proxima_faixa_aberta()

    def obter_proxima_faixa_aberta(self):
        linha = self.cliente_banco.executar_query(
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

    def atualizar_progresso_faixa(self, faixa_id, paginas_concluidas, novos, motivo, observacao):
        # 6. SEM commit de propósito: quem chama segura a transação junto com os imóveis
        self.cliente_banco.executar_query(
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
                paginas_concluidas, novos, 1 if motivo else 0, motivo,
                observacao, datetime.now().isoformat(), faixa_id,
            ),
        )

    def obter_resumo_por_situacao(self):
        return self.cliente_banco.executar_query(
            """
            SELECT finalizado, COALESCE(motivo_finalizacao, '-'), COUNT(*), SUM(imoveis_novos)
            FROM faixas_preco
            GROUP BY finalizado, motivo_finalizacao
            """
        ).fetchall()

    def obter_progresso_faixa(self, faixa_id):
        return self.cliente_banco.executar_query(
            "SELECT numero_paginas, finalizado FROM faixas_preco WHERE id = ?", (faixa_id,)
        ).fetchone()