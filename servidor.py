# Jarvis RESERVA: mora na nuvem (Render, grátis) e cuida da Miss Valen quando o Mac está desligado.
#   * conversa pelo app do iPhone (ouvido e cérebro: Groq, grátis; voz: Microsoft Antonio, grátis)
#   * se o Mac sumir por 5 minutos, ele assume os avisos da rotina e liga para o app
# Variáveis (Render): APP_KEY, GROQ_KEY, VAPID_PEM, ORIGEM, ROTINA_JSON, INSCRICOES_JSON
import base64
import datetime as dt
import io
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

APP_KEY = os.environ.get('APP_KEY', '')
GROQ = os.environ.get('GROQ_KEY', '')
ORIGEM = os.environ.get('ORIGEM', '')
FUSO = ZoneInfo('America/Sao_Paulo')
TRATAMENTO = 'Miss Valen'
PERSONA = (f'Você é o J.A.R.V.I.S., assistente pessoal da {TRATAMENTO} (uma menina brasileira). Fale português do Brasil, '
           'curto (no máximo 2 frases), elegante e um pouco espirituoso, como um mordomo britânico. Você é o Jarvis RESERVA: '
           'o Mac dela está desligado, então você não consegue mexer no computador, música ou casa; só conversar, '
           'lembrar a rotina e ajudar com dúvidas. Nunca invente que fez algo no computador.')

def _json_env(nome):
    try:
        return json.loads(os.environ.get(nome, '') or '[]')
    except ValueError:
        return []


# rotina da semana (itens com dias 0=segunda..6=domingo e "inicio" HH:MM) e inscrições do app;
# começam com o que o Mac deixou nas variáveis e são atualizadas a cada minuto pelo Mac.
ESTADO = {'ultimo_mac': 0.0, 'semana': _json_env('ROTINA_JSON'), 'inscricoes': _json_env('INSCRICOES_JSON'), 'avisados': set()}


def proximos(horas=36):
    agora = dt.datetime.now(FUSO)
    saida = []
    for d in range(3):
        dia = agora + dt.timedelta(days=d)
        for item in ESTADO['semana']:
            if dia.weekday() in item.get('dias', []) and item.get('aviso') != 'bom_dia':
                h, m = map(int, item['inicio'].split(':'))
                quando = dia.replace(hour=h, minute=m, second=0, microsecond=0)
                if agora - dt.timedelta(minutes=15) <= quando <= agora + dt.timedelta(hours=horas):
                    saida.append((quando, item['titulo']))
    return sorted(saida)
CHAMADAS, AUDIOS = {}, {}
_kokoro, _trava = None, threading.Lock()


# ------------------------------------------------------------------ voz, ouvido e cérebro
def sintetizar(texto):
    """Voz grátis da Microsoft (Antonio, português do Brasil), um pouco mais grave, estilo mordomo. Devolve MP3."""
    import asyncio
    import edge_tts

    async def gerar():
        som = b''
        async for parte in edge_tts.Communicate(texto, 'pt-BR-AntonioNeural', rate='+4%', pitch='-6Hz').stream():
            if parte['type'] == 'audio':
                som += parte['data']
        return som
    return asyncio.run(gerar())


def ouvir(wav):
    r = requests.post('https://api.groq.com/openai/v1/audio/transcriptions', headers={'Authorization': f'Bearer {GROQ}'},
                      files={'file': ('fala.wav', wav, 'audio/wav')},
                      data={'model': 'whisper-large-v3-turbo', 'language': 'pt'}, timeout=30)
    r.raise_for_status()
    return r.json().get('text', '').strip()


def pensar(conversa):
    d = dt.datetime.now(FUSO)
    dias = ['segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira', 'sexta-feira', 'sábado', 'domingo']
    agora = f'{dias[d.weekday()]}, {d:%d/%m/%Y %H:%M}'
    rotina = '; '.join(f'{dias[q.weekday()]} {q:%H:%M} {t}' for q, t in proximos()[:12]) or 'sem dados'
    msgs = [{'role': 'system', 'content': f'{PERSONA}\nAgora: {agora} (Brasília). Próximos itens da rotina: {rotina}.'}]
    msgs += [{'role': 'user' if q == 'voce' else 'assistant', 'content': t} for q, t in conversa[-10:]]
    r = requests.post('https://api.groq.com/openai/v1/chat/completions', headers={'Authorization': f'Bearer {GROQ}'},
                      json={'model': 'openai/gpt-oss-120b', 'reasoning_effort': 'low', 'messages': msgs, 'temperature': 0.5, 'max_tokens': 500},
                      timeout=30)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content'].strip()


# ------------------------------------------------------------------ ligações para o app (Web Push)
def avisar(titulo, corpo, cid=None, toque=0):
    from pywebpush import webpush
    pem = os.environ.get('VAPID_PEM', '').replace('\\n', '\n')
    if not pem:
        return False
    arq = '/tmp/vapid.pem'
    with open(arq, 'w') as f:
        f.write(pem)
    ok = False
    for insc in ESTADO['inscricoes']:
        try:
            webpush(insc, json.dumps({'titulo': titulo, 'corpo': corpo, 'c': cid, 'toque': toque}), vapid_private_key=arq,
                    vapid_claims={'sub': 'mailto:jarvis-da-valen@example.com'}, ttl=120, headers={'Urgency': 'high'})
            ok = True
        except Exception as erro:
            print('(push falhou:', str(erro)[:80], ')', flush=True)
    return ok


def ligar(mensagem):
    cid = secrets.token_urlsafe(18)
    CHAMADAS[cid] = {'conversa': [], 'inicio': mensagem, 'criada': time.time()}
    if not avisar('📞 JARVIS está te ligando', 'Toque para atender.', cid):
        return

    def tocar():
        for n in range(2, 9):
            time.sleep(4)
            ch = CHAMADAS.get(cid)
            if not ch or ch.get('atendida'):
                return
            avisar('📞 JARVIS está te ligando', 'Toque para atender.', cid, n)
    threading.Thread(target=tocar, daemon=True).start()


def vigia():
    """Se o Mac sumiu há 5 min, a reserva liga 10 min antes de cada item da rotina."""
    while True:
        time.sleep(30)
        if time.time() - ESTADO['ultimo_mac'] < 300:
            continue
        agora = dt.datetime.now(FUSO)
        for inicio, titulo in proximos(1):
            quando = inicio - dt.timedelta(minutes=10)
            chave = f'{inicio:%Y%m%d%H%M}{titulo}'
            if chave not in ESTADO['avisados'] and quando <= agora < quando + dt.timedelta(minutes=3):
                ESTADO['avisados'].add(chave)
                ligar(f'Olá, {TRATAMENTO}. Aqui é o Jarvis reserva. Em 10 minutos: {titulo}, às {inicio:%H:%M}.')


def limpeza():
    while True:
        time.sleep(60)
        for t, (c, _) in list(AUDIOS.items()):
            if time.time() - c > 600:
                AUDIOS.pop(t, None)
        for k, ch in list(CHAMADAS.items()):
            if time.time() - ch['criada'] > 3600:
                CHAMADAS.pop(k, None)


# ------------------------------------------------------------------ servidor (mesmas rotas do Mac)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        o = self.headers.get('Origin', '')
        if ORIGEM and o == ORIGEM:
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Vary', 'Origin')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Jarvis-Chave')
        self.end_headers()

    def _env(self, corpo, tipo='application/json; charset=utf-8', codigo=200):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(corpo)

    def _chave(self):
        import hmac
        return bool(APP_KEY) and hmac.compare_digest(self.headers.get('X-Jarvis-Chave', ''), APP_KEY)

    def _audio(self, texto):
        t = secrets.token_urlsafe(16)
        AUDIOS[t] = (time.time(), sintetizar(texto))
        return f'/audio/{t}.mp3'

    def do_HEAD(self):   # o "cutucão" para não dormir
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/ping':
            return self._env({'jarvis': True, 'reserva': True})
        if self.path.startswith('/audio/'):
            t = self.path[7:].removesuffix('.mp3')
            if t in AUDIOS:
                return self._env(AUDIOS[t][1], 'audio/mpeg')
        if self.path == '/':
            return self._env(b'J.A.R.V.I.S. reserva online.', 'text/plain; charset=utf-8')
        self._env({'erro': 'nada aqui'}, codigo=404)

    def do_POST(self):
        tam = int(self.headers.get('Content-Length', 0) or 0)
        if tam > 8_000_000:
            return self._env({'erro': 'grande'}, codigo=413)
        corpo = self.rfile.read(tam)
        rota, _, q = self.path.partition('?')
        cid = dict(p.split('=', 1) for p in q.split('&') if '=' in p).get('c', '')
        if rota == '/api/sincronizar':   # o Mac manda rotina + inscrições a cada minuto ("estou vivo")
            if not self._chave():
                return self._env({'erro': 'senha'}, codigo=403)
            d = json.loads(corpo or b'{}')
            ESTADO.update(ultimo_mac=time.time(), semana=d.get('semana', ESTADO['semana']), inscricoes=d.get('inscricoes', ESTADO['inscricoes']))
            return self._env({'ok': True})
        if rota == '/api/chamar':
            if not self._chave():
                return self._env({'erro': 'senha'}, codigo=403)
            cid = secrets.token_urlsafe(18)
            CHAMADAS[cid] = {'conversa': [], 'inicio': f'Jarvis reserva aqui, {TRATAMENTO}. O Mac está desligado, mas eu estou com você. Pode falar.',
                             'criada': time.time(), 'atendida': True}
            return self._env({'c': cid})
        ch = CHAMADAS.get(cid)
        if ch is None:
            return self._env({'fim': True, 'texto': 'Esta chamada já terminou.'})
        try:
            if rota == '/app/atender':
                ch['atendida'] = True
                ch['conversa'].append(('jarvis', ch['inicio']))
                return self._env({'texto': ch['inicio'], 'audio': self._audio(ch['inicio'])})
            if rota == '/app/fala':
                texto = ouvir(corpo) if len(corpo) > 2000 else ''
                if not texto:
                    return self._env({'voce': '', 'texto': 'Não entendi. Pode repetir?', 'audio': self._audio('Não entendi. Pode repetir?')})
                fim = any(p in texto.lower() for p in ('tchau', 'pode desligar', 'até mais'))
                ch['conversa'].append(('voce', texto))
                fala = f'Até mais, {TRATAMENTO}!' if fim else pensar(ch['conversa'])
                ch['conversa'].append(('jarvis', fala))
                if fim:
                    CHAMADAS.pop(cid, None)
                return self._env({'voce': texto, 'texto': fala, 'audio': self._audio(fala), 'fim': fim})
            if rota == '/app/fim':
                CHAMADAS.pop(cid, None)
                return self._env({'fim': True})
        except Exception as erro:
            print('(erro:', str(erro)[:120], ')', flush=True)
            return self._env({'texto': 'Tive um probleminha na nuvem. Tenta de novo?', 'erro': True})
        self._env({'erro': 'nada aqui'}, codigo=404)


if __name__ == '__main__':
    threading.Thread(target=vigia, daemon=True).start()
    threading.Thread(target=limpeza, daemon=True).start()
    print('Jarvis reserva online na porta', os.environ.get('PORT', '7860'), flush=True)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '7860'))), H).serve_forever()
