import logging

registro_log = logging.getLogger("motor_de_raspagem")

# 1. Ordem das colunas de dados (sem o id autoincrement). O INSERT é montado a partir daqui,
# e o extrator devolve um dicionário com exatamente essas chaves
COLUNAS_RAW_IMOVEIS = (
    "id_anuncio", "codigo_anunciante", "id_anunciante",
    "tipo_imovel", "operacao",
    "preco", "valor_condominio", "valor_iptu",
    "area_m2", "quartos", "banheiros", "suites", "vagas",
    "rua", "numero", "bairro", "cidade", "estado", "cep", "latitude", "longitude",
    "caracteristicas", "fotos", "descricao",
    "url", "status_anuncio",
    "data_publicacao", "data_atualizacao_site", "data_coleta",
)


class Repositorio_RawImoveis:
    def __init__(self, cliente_banco):
        self.cliente_banco = cliente_banco

    def criar_tabela(self):
        # 2. Tabela bruta: guarda o que o site mostrou, sem limpar nem normalizar.
        # fotos = hashes separados por vírgula; caracteristicas = códigos separados por vírgula
        with self.cliente_banco.conexao:
            self.cliente_banco.executar_query(
                """
                CREATE TABLE IF NOT EXISTS raw_imoveis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    id_anuncio TEXT NOT NULL UNIQUE,
                    codigo_anunciante TEXT,
                    id_anunciante TEXT,
                    tipo_imovel TEXT,
                    operacao TEXT,
                    preco INTEGER,
                    valor_condominio INTEGER,
                    valor_iptu INTEGER,
                    area_m2 INTEGER,
                    quartos INTEGER,
                    banheiros INTEGER,
                    suites INTEGER,
                    vagas INTEGER,
                    rua TEXT,
                    numero TEXT,
                    bairro TEXT,
                    cidade TEXT,
                    estado TEXT,
                    cep TEXT,
                    latitude REAL,
                    longitude REAL,
                    caracteristicas TEXT,
                    fotos TEXT,
                    descricao TEXT,
                    url TEXT,
                    status_anuncio TEXT,
                    data_publicacao TEXT,
                    data_atualizacao_site TEXT,
                    data_coleta TEXT
                )
                """
            )

    def adicionar_imovel(self, dados_imovel):
        # 3. SEM commit de propósito: o motor grava o raw e a operação na mesma transação.
        # INSERT OR IGNORE: o mesmo id_anuncio nunca duplica; devolve True só se inseriu de fato
        colunas = ", ".join(COLUNAS_RAW_IMOVEIS)
        interrogacoes = ", ".join("?" for _ in COLUNAS_RAW_IMOVEIS)
        valores = tuple(dados_imovel.get(coluna) for coluna in COLUNAS_RAW_IMOVEIS)

        cursor = self.cliente_banco.executar_query(
            f"INSERT OR IGNORE INTO raw_imoveis ({colunas}) VALUES ({interrogacoes})",
            valores,
        )
        return cursor.rowcount == 1

    def obter_por_id_anuncio(self, id_anuncio):
        linha = self.cliente_banco.executar_query(
            f"SELECT {', '.join(COLUNAS_RAW_IMOVEIS)} FROM raw_imoveis WHERE id_anuncio = ?",
            (id_anuncio,),
        ).fetchone()
        if linha is None:
            return None
        return dict(zip(COLUNAS_RAW_IMOVEIS, linha))

    def contar_imoveis(self):
        return self.cliente_banco.executar_query("SELECT COUNT(*) FROM raw_imoveis").fetchone()[0]