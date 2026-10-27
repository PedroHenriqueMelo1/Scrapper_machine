from engine.MotorDeRaspagem_v1 import MotorDeRaspagem





if __name__ == "__main__":
    # 15. Ctrl+C encerra limpo: o que já foi gravado fica no banco
    try:
        with MotorDeRaspagem() as motor:
            motor.raspar_paginas(pagina_inicial=3, pagina_final=50)
    except KeyboardInterrupt:
        registro_log.info("Interrompido pelo usuário. Páginas já processadas estão salvas.")