from engine.motores.MotorDeRaspagem_Crawler_v1 import MotorDeRaspagem, configurar_log





if __name__ == "__main__":
    # 15. Ctrl+C encerra limpo: o que já foi gravado fica no banco
    try:
        configurar_log()
        with MotorDeRaspagem() as motor:
         motor.executar()  # primeira faixa; pode deixar, não duplica
    except KeyboardInterrupt:
        registro_log.info("Interrompido pelo usuário. Páginas já processadas estão salvas.")