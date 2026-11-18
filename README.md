# Cortador de Lives com IA — pacote web

Interface responsiva para celular e computador. Suporta upload de vídeo e link público do YouTube, transcrição com Faster-Whisper, seleção de trechos com Gemini e montagem de MP4 vertical com FFmpeg.

## Publicação
1. Envie esta pasta para um repositório GitHub.
2. No Render, crie um Web Service a partir do repositório e selecione Docker.
3. Configure `GEMINI_API_KEY` como variável secreta.
4. Publique e abra a URL HTTPS gerada no Android ou computador.

O arquivo `render.yaml` sugere uma configuração inicial gratuita. O plano gratuito do Render tem recursos limitados, suspende serviços sem uso e usa disco efêmero. Isso significa que o primeiro acesso pode demorar, trabalhos podem ser interrompidos e arquivos gerados não são armazenamento permanente. Processamento de vídeos longos pode exceder a memória/CPU disponível; para uso confiável e contínuo pode ser necessário um plano pago.

## Configuração
- `GEMINI_API_KEY`: chave da API Gemini (obrigatória).
- `GEMINI_MODEL`: padrão `gemini-2.5-flash`.
- `WHISPER_MODEL`: padrão `tiny` para reduzir consumo de memória.
- `MAX_UPLOAD_MB`: padrão de publicação 500 MB.

## Segurança e uso
A chave configurada no servidor não é exibida no navegador. Use HTTPS e não compartilhe a URL administrativa se futuramente adicionar autenticação. Use somente vídeos que você possui ou tem autorização para processar e respeite os termos do YouTube. Alguns vídeos podem não estar disponíveis para download via yt-dlp.

## Execução local
Instale Python 3.11 e FFmpeg, rode `pip install -r requirements.txt` e depois `python app.py`.
