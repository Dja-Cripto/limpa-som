# Limpa Som

**Teste de microfone que mede o chiado, corrige o que o computador permite e explica, em palavras simples, o que ainda falta.**

Programa para Windows que grava o seu microfone, mede ruído, nível, zumbido, apitos, estouro de áudio e largura de banda, ajusta o ganho e gera uma versão com limpeza digital para você comparar. No fim, diz com clareza o que foi corrigido, o que você ainda precisa configurar e o que **não** é configuração (hardware, cabo, tomada ou sala).

Tudo roda no seu computador. Nenhum áudio é enviado para a internet.

## O que ele faz

- **Teste guiado (cerca de 5 minutos):** grava silêncio e a sua voz e mede relação sinal/ruído, zumbido, apitos, estouro de áudio, largura de banda e ganho automático.
- **Ajuste de ganho seguro:** testa combinações de sensibilidade do microfone e volume de entrada do Windows e fica com a que dá menos chiado. Guarda as configurações originais e volta sozinho ao original se o resultado piorar.
- **Dois testes de causa:** modo exclusivo (efeitos do Windows ou do driver) e isolamento da fonte (o ruído nasce no computador ou antes dele).
- **Nota de 0 a 10**, baseada na relação sinal/ruído, com descontos por problemas. A nota da limpeza digital nunca passa de 2,5 pontos acima da real: limpeza digital esconde chiado, mas não troca o microfone.
- **Equalizador do microfone**, criado a partir da sua voz e aplicado no Equalizer APO só depois da sua confirmação.
- **Relatório em HTML** com gráficos e os áudios (antes, depois e limpo) para você ouvir.
- **Ajuste ao vivo** com medidor de nível e **limpeza de arquivo** de áudio ou vídeo já gravado.

## Decisões de projeto

- **Honestidade no diagnóstico.** O programa separa o que é configuração do que é hardware, em vez de prometer que tudo se resolve por software.
- **Mudanças reversíveis.** Antes de mexer no ganho, guarda os valores originais. Se algo der erro no meio, volta ao original sozinho. Existe um botão "Restaurar configurações do microfone".
- **Privacidade.** Processamento local e sem envio de áudio.
- **Não instala drivers e não mexe no registro do Windows.**

## Como usar

Requisito: Windows e Python 3.12 ou mais novo.

1. Dê dois cliques em `INICIAR.bat`. Na primeira vez ele prepara o ambiente (1 a 2 minutos, precisa de internet).
2. Escolha o microfone e clique em **Testar o microfone e corrigir o chiado**.
3. Siga as instruções na tela. Feche Discord, OBS, Zoom e Teams antes, porque eles seguram o microfone.

Prefere texto, sem janela? `INICIAR.bat --console`.

O guia completo, em português, está em [`LEIA-ME.txt`](LEIA-ME.txt).

## Testes

```bash
python -m pip install pytest
python -m pytest tests -q
```

A suíte tem 135 testes (processamento de sinal, pontuação, diagnóstico, equalizador, fluxo em tempo real, interface e segurança das mudanças).

## Tecnologias

Python, NumPy, SciPy, sounddevice, soundfile, pycaw e Tkinter.

## Estado atual

Em uso e em evolução. Os testes automáticos cobrem a lógica de medição e de decisão. O comportamento com placas de áudio e mesas de som diferentes ainda depende de testes em mais equipamentos.

## Sobre

Criado por **Daniel de Jesus** (estudante de Análise e Desenvolvimento de Sistemas), com IA como parceira de desenvolvimento e revisão, testes e decisões minhas.
[LinkedIn](https://www.linkedin.com/in/daniel-de-jesus-alves/) · [Portfólio](https://dja-portfolio.vercel.app)

> Código publicado para fins de portfólio. Sem licença de uso: todos os direitos reservados.
