"""Pruebas unitarias para investigar_a_fondo, extracción de bloques <pre><code> y reglas en vision.py y cerebro.py."""
from types import SimpleNamespace
import pytest

import vision
from jarvis import cerebro, herramientas


REGLA_INVESTIGACION = (
    "Cuando el usuario pida investigar cómo configurar, conectar o ejecutar una herramienta técnica, "
    "NUNCA respondas de memoria si hay dudas ni abras el navegador visual. Ejecutá primero "
    "investigar_a_fondo para leer la documentación oficial completa y los bloques de código reales "
    "antes de responder o de usar ejecutar_comando."
)


def _mock_entorno_web(monkeypatch, resultados_ddgs, paginas_html, llamadas_get=None):
    """Configura mocks para DDGS y requests.get."""
    class DDGSFalso:
        def text(self, consulta, max_results=4):
            return resultados_ddgs[:max_results]

    class RequestsFalso:
        @staticmethod
        def get(url, timeout=None, headers=None):
            if llamadas_get is not None:
                llamadas_get.append({"url": url, "timeout": timeout, "headers": headers})
            html = paginas_html.get(url, "")
            return SimpleNamespace(status_code=200, text=html)

    monkeypatch.setattr(herramientas, "DDGS", DDGSFalso)
    monkeypatch.setattr(herramientas, "requests", RequestsFalso)


# ---------------------------------------------------------------- extracción HTML y <pre><code>

def test_investigar_a_fondo_extrae_pre_code_y_elimina_ruido(monkeypatch):
    url_doc = "https://docs.tryton.org/projects/server/en/latest/topics/configuration.html"
    html_complejo = """
    <html>
      <head>
        <style>body { color: red; }</style>
        <script>console.log("ruido js");</script>
      </head>
      <body>
        <header>Menú superior de navegación ruidoso</header>
        <nav>Links de barra lateral</nav>
        <main>
          <h1>Configuración de Trytond</h1>
          <h2>Conexión a PostgreSQL</h2>
          <p>Editá el archivo de configuración principal antes de iniciar el servicio.</p>
          <ul>
            <li>Definir URI de base de datos</li>
            <li>Inicializar esquema con trytond-admin</li>
          </ul>
          <pre><code>[database]
uri = postgresql://tryton:tryton@localhost:5432/gnuhealth
path = /var/lib/trytond</code></pre>
          <h3>Comando de inicialización</h3>
          <pre><code>trytond-admin -c /etc/tryton/trytond.conf -d gnuhealth --all</code></pre>
        </main>
        <footer>Copyright pie de página ruidoso</footer>
      </body>
    </html>
    """
    llamadas = []
    _mock_entorno_web(
        monkeypatch,
        resultados_ddgs=[{"title": "Tryton Configuration", "href": url_doc}],
        paginas_html={url_doc: html_complejo},
        llamadas_get=llamadas,
    )

    resultado = herramientas.investigar_a_fondo("configurar trytond postgresql")

    # Verifica llamada HTTP con timeout=4 y User-Agent
    assert len(llamadas) == 1
    assert llamadas[0]["url"] == url_doc
    assert llamadas[0]["timeout"] == 4
    assert "Mozilla/5.0" in llamadas[0]["headers"]["User-Agent"]

    # Verifica URL fuente y estructura extraída
    assert url_doc in resultado
    assert "# Configuración de Trytond" in resultado
    assert "## Conexión a PostgreSQL" in resultado
    assert "### Comando de inicialización" in resultado
    assert "- Definir URI de base de datos" in resultado
    assert "uri = postgresql://tryton:tryton@localhost:5432/gnuhealth" in resultado
    assert "trytond-admin -c /etc/tryton/trytond.conf -d gnuhealth --all" in resultado

    # Verifica eliminación de etiquetas de ruido
    assert "ruido js" not in resultado
    assert "color: red" not in resultado
    assert "Menú superior de navegación ruidoso" not in resultado
    assert "Links de barra lateral" not in resultado
    assert "Copyright pie de página ruidoso" not in resultado


# ---------------------------------------------------------------- límite de 12.000 caracteres y prioridad <pre><code>

def test_investigar_a_fondo_respeta_limite_12000_caracteres_y_prioriza_codigo(monkeypatch):
    resultados = [
        {"title": f"Doc {i}", "href": f"https://postgresql.org/docs/parte-{i}"}
        for i in range(4)
    ]
    prosa_larga = "<p>" + ("Texto introductorio extenso sobre arquitectura. " * 350) + "</p>"
    codigo_critico = "<pre><code>ALTER SYSTEM SET shared_buffers = '4GB';</code></pre>"
    paginas = {
        r["href"]: f"<html><body><h1>Título {i}</h1>{prosa_larga}{codigo_critico}</body></html>"
        for i, r in enumerate(resultados)
    }
    _mock_entorno_web(monkeypatch, resultados, paginas)

    resultado = herramientas.investigar_a_fondo("postgresql tuning")
    assert herramientas.MAX_CARACTERES_INVESTIGACION == 12_000
    assert len(resultado) == 12_000
    assert "ALTER SYSTEM SET shared_buffers = '4GB';" in resultado


# ---------------------------------------------------------------- priorización de fuentes oficiales

def test_investigar_a_fondo_prioriza_fuentes_oficiales(monkeypatch):
    resultados = [
        {"title": "Blog personal", "href": "https://miblog-random.com/post-1"},
        {"title": "Foro comercial", "href": "https://respuestas-rapidas.net/hilo"},
        {"title": "GNU Health Wiki", "href": "https://wiki.gnuhealth.org/index.php/Installation"},
        {"title": "PostgreSQL Official", "href": "https://www.postgresql.org/docs/current/auth.html"},
        {"title": "Tryton Official", "href": "https://docs.tryton.org/en/latest/"},
        {"title": "GitHub Repo", "href": "https://github.com/tryton/trytond"},
    ]
    paginas = {
        r["href"]: f"<html><body><p>Contenido de {r['href']}</p></body></html>"
        for r in resultados
    }
    llamadas = []
    _mock_entorno_web(monkeypatch, resultados, paginas, llamadas_get=llamadas)

    resultado = herramientas.investigar_a_fondo("instalar gnuhealth", solo_fuentes_oficiales=True)

    urls_consultadas = [c["url"] for c in llamadas]
    assert len(urls_consultadas) == 4
    assert "https://wiki.gnuhealth.org/index.php/Installation" in urls_consultadas
    assert "https://www.postgresql.org/docs/current/auth.html" in urls_consultadas
    assert "https://docs.tryton.org/en/latest/" in urls_consultadas
    assert "https://github.com/tryton/trytond" in urls_consultadas
    assert "https://miblog-random.com/post-1" not in urls_consultadas
    assert "https://wiki.gnuhealth.org/index.php/Installation" in resultado


# ---------------------------------------------------------------- ocultamiento de secretos

def test_investigar_a_fondo_oculta_secretos(monkeypatch):
    url = "https://github.com/org/repo/blob/main/README.md"
    html = """
    <html><body>
      <pre><code>export GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwx</code></pre>
    </body></html>
    """
    _mock_entorno_web(
        monkeypatch,
        resultados_ddgs=[{"title": "Repo", "href": url}],
        paginas_html={url: html},
    )

    resultado = herramientas.investigar_a_fondo("token config")
    assert "ghp_1234567890" not in resultado
    assert "GITHUB_TOKEN=***" in resultado


# ---------------------------------------------------------------- reglas en vision.py y cerebro.py

def test_investigar_a_fondo_es_no_bloqueante_en_vision():
    assert "investigar_a_fondo" in vision.NO_BLOQUEANTES


def test_regla_estricta_presente_en_vision_y_cerebro():
    instrucciones_vision = vision.instrucciones(con_video=True, con_herramientas=True)
    assert REGLA_INVESTIGACION in instrucciones_vision
    assert REGLA_INVESTIGACION in cerebro.INSTRUCCIONES



# ---------------------------------------------------------------- YouTube: extracción de ID

@pytest.mark.parametrize("url,esperado", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL123", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])
def test_extraer_id_youtube_formatos_validos(url, esperado):
    assert herramientas.extraer_id_youtube(url) == esperado


@pytest.mark.parametrize("url", [
    "https://docs.tryton.org/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/channel/UC123",
    "https://www.youtube.com/watch?v=corto",
    "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
    "configurar postgresql",
    "",
])
def test_extraer_id_youtube_rechaza_no_videos(url):
    assert herramientas.extraer_id_youtube(url) == ""


# ---------------------------------------------------------------- YouTube: mocks de transcripción

def _mock_youtube(monkeypatch, transcripciones, llamadas=None):
    """Mock de YouTubeTranscriptApi.get_transcript: id -> lista de fragmentos o excepción."""
    class YTFalso:
        @staticmethod
        def get_transcript(id_video, languages=None):
            if llamadas is not None:
                llamadas.append({"id": id_video, "languages": languages})
            valor = transcripciones.get(id_video)
            if isinstance(valor, Exception):
                raise valor
            if valor is None:
                raise RuntimeError("TranscriptsDisabled")
            return valor

    monkeypatch.setattr(herramientas, "YouTubeTranscriptApi", YTFalso)


def test_investigar_a_fondo_incluye_transcripcion_de_youtube(monkeypatch):
    url_doc = "https://www.postgresql.org/docs/current/auth-pg-hba-conf.html"
    url_yt = "https://www.youtube.com/watch?v=abcDEF12345"
    url_yt_corto = "https://youtu.be/XYZxyz98765"
    llamadas_get, llamadas_yt = [], []
    _mock_entorno_web(
        monkeypatch,
        resultados_ddgs=[
            {"title": "pg_hba.conf", "href": url_doc},
            {"title": "Tutorial PostgreSQL", "href": url_yt},
            {"title": "Short PostgreSQL", "href": url_yt_corto},
        ],
        paginas_html={url_doc: "<html><body><pre><code>host all all 0.0.0.0/0 md5</code></pre></body></html>"},
        llamadas_get=llamadas_get,
    )
    _mock_youtube(monkeypatch, {
        "abcDEF12345": [{"text": "Primero editamos", "start": 0.0}, {"text": "el archivo pg_hba.conf", "start": 1.5}],
        "XYZxyz98765": [{"text": "Reiniciá el servicio", "start": 0.0}],
    }, llamadas=llamadas_yt)

    resultado = herramientas.investigar_a_fondo("configurar pg_hba postgresql")

    # Los videos no se descargan como HTML: solo la página de documentación.
    assert [c["url"] for c in llamadas_get] == [url_doc]
    assert [c["id"] for c in llamadas_yt] == ["abcDEF12345", "XYZxyz98765"]
    assert "es" in llamadas_yt[0]["languages"] and "en" in llamadas_yt[0]["languages"]

    assert "host all all 0.0.0.0/0 md5" in resultado
    assert f"Fuente: {url_yt}" in resultado
    assert "[Transcripción del video de YouTube]" in resultado
    assert "Primero editamos el archivo pg_hba.conf" in resultado
    assert "Reiniciá el servicio" in resultado


def test_investigar_a_fondo_ignora_video_sin_transcripcion(monkeypatch):
    url_doc = "https://wiki.gnuhealth.org/Installation"
    url_yt = "https://youtu.be/sinSubs0001"
    _mock_entorno_web(
        monkeypatch,
        resultados_ddgs=[{"title": "Video", "href": url_yt}, {"title": "Wiki", "href": url_doc}],
        paginas_html={url_doc: "<html><body><p>Instalá con pip install gnuhealth</p></body></html>"},
    )
    _mock_youtube(monkeypatch, {"sinSubs0001": RuntimeError("NoTranscriptFound")})

    resultado = herramientas.investigar_a_fondo("instalar gnuhealth")
    assert "Instalá con pip install gnuhealth" in resultado
    assert url_yt not in resultado


def test_leer_video_youtube_con_url_directa(monkeypatch):
    llamadas = []
    _mock_youtube(monkeypatch, {
        "dQw4w9WgXcQ": [{"text": "Hola,", "start": 0}, {"text": "hoy configuramos  Tryton", "start": 2}],
    }, llamadas=llamadas)

    class DDGSNoUsar:
        def text(self, *a, **k):
            raise AssertionError("No debería buscar si ya hay URL")

    monkeypatch.setattr(herramientas, "DDGS", DDGSNoUsar)

    resultado = herramientas.leer_video_youtube("https://youtu.be/dQw4w9WgXcQ")
    assert llamadas[0]["id"] == "dQw4w9WgXcQ"
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in resultado
    assert "Hola, hoy configuramos Tryton" in resultado


def test_leer_video_youtube_con_busqueda(monkeypatch):
    consultas = []

    class DDGSFalso:
        def text(self, consulta, max_results=10):
            consultas.append(consulta)
            return [
                {"title": "Blog", "href": "https://blog.ejemplo.com/tryton"},
                {"title": "Instalar Tryton 7", "href": "https://www.youtube.com/watch?v=tryton70000"},
            ]

    monkeypatch.setattr(herramientas, "DDGS", DDGSFalso)
    _mock_youtube(monkeypatch, {"tryton70000": [{"text": "Instalamos trytond con pip"}]})

    resultado = herramientas.leer_video_youtube("instalar tryton 7")
    assert "youtube.com" in consultas[0]
    assert "(Instalar Tryton 7)" in resultado
    assert "Instalamos trytond con pip" in resultado


def test_leer_video_youtube_sin_resultados_ni_transcripcion(monkeypatch):
    class DDGSVacio:
        def text(self, consulta, max_results=10):
            return [{"title": "Blog", "href": "https://blog.ejemplo.com"}]

    monkeypatch.setattr(herramientas, "DDGS", DDGSVacio)
    assert "No encontré ningún video" in herramientas.leer_video_youtube("algo raro")

    _mock_youtube(monkeypatch, {"dQw4w9WgXcQ": RuntimeError("TranscriptsDisabled")})
    resultado = herramientas.leer_video_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert "No pude leer el video" in resultado
    assert "español o inglés" in resultado


def test_leer_video_youtube_trunca_y_oculta_secretos(monkeypatch):
    largo = [{"text": "palabra " * 10}] * 600  # ~48.000 caracteres
    largo = largo + [{"text": "token ghp_1234567890abcdefghijklmnopqrstuvwx"}]
    _mock_youtube(monkeypatch, {"dQw4w9WgXcQ": [{"text": "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwx"}] + largo})

    resultado = herramientas.leer_video_youtube("https://youtu.be/dQw4w9WgXcQ")
    assert "ghp_1234567890" not in resultado
    assert "truncada" in resultado
    assert len(resultado) <= herramientas.MAX_CARACTERES_INVESTIGACION + 200


def test_leer_video_youtube_sin_libreria(monkeypatch):
    monkeypatch.setattr(herramientas, "YouTubeTranscriptApi", None)
    resultado = herramientas.leer_video_youtube("https://youtu.be/dQw4w9WgXcQ")
    assert "youtube-transcript-api" in resultado


def test_leer_video_youtube_api_nueva_con_fetch(monkeypatch):
    """youtube-transcript-api >= 1.0 no tiene get_transcript: se usa la instancia con fetch()."""
    class Fragmento:
        def __init__(self, text):
            self.text = text

    class YTNueva:
        def fetch(self, id_video, languages=None):
            assert id_video == "dQw4w9WgXcQ"
            return [Fragmento("API"), Fragmento("nueva")]

    monkeypatch.setattr(herramientas, "YouTubeTranscriptApi", YTNueva)
    assert "API nueva" in herramientas.leer_video_youtube("https://youtu.be/dQw4w9WgXcQ")


def test_leer_video_youtube_es_no_bloqueante_y_registrada():
    assert "leer_video_youtube" in vision.NO_BLOQUEANTES
    assert "leer_video_youtube" in herramientas.REGISTRO


# ---------------------------------------------------------------- corrector fonético y multihilo

@pytest.mark.parametrize("entrada,esperado", [
    ("instalar genugel en servidor", "instalar GNU Health en servidor"),
    ("configurar gnu g y genu health", "configurar GNU Health y GNU Health"),
    ("error en geneohealth", "error en GNU Health"),
    ("conectar divi builder a posgre", "conectar DBeaver a PostgreSQL"),
    ("abrir debeaver o d beaver", "abrir DBeaver o DBeaver"),
    ("servidor triton y traiton con postgre", "servidor Tryton y Tryton con PostgreSQL"),
    ("consulta en PostgreSQL activa", "consulta en PostgreSQL activa"),
])
def test_normalizar_consulta_tecnica(entrada, esperado):
    assert herramientas.normalizar_consulta_tecnica(entrada) == esperado


def test_investigar_a_fondo_aplica_corrector_fonetico(monkeypatch):
    consultas_vistas = []

    class DDGSFalso:
        def text(self, consulta, max_results=4):
            consultas_vistas.append(consulta)
            return []

    monkeypatch.setattr(herramientas, "DDGS", DDGSFalso)
    herramientas.investigar_a_fondo("conectar divi builder con genugel y triton en posgre")
    assert consultas_vistas == ["conectar DBeaver con GNU Health y Tryton en PostgreSQL"]


# ---------------------------------------------------------------- temperatura 0.1 y Ley de Precisión Absoluta

def test_temperatura_01_y_ley_precision_absoluta():
    v = vision.VisionEnVivo(cliente=None, microfono=None, parlante=None, con_video=True, con_herramientas=True)
    cfg = v.configuracion()
    assert cfg.temperature == 0.1
    assert cerebro.TEMPERATURA == 0.1

    texto_vision = vision.instrucciones(con_video=True, con_herramientas=True)
    for destino in (texto_vision, cerebro.INSTRUCCIONES):
        assert "LEY DE PRECISIÓN ABSOLUTA:" in destino
        assert "1. PROHIBIDO ADIVINAR O DAR RODEOS:" in destino
        assert "2. FORMATO DE RESPUESTA OBLIGATORIO (1, 2, 3):" in destino
        assert "3. EJECUCIÓN DIRECTA:" in destino


# ---------------------------------------------------------------- ejecutar_wsl, ejecutar_ssh_vm, inspeccionar_pantalla_hd

def test_ejecutar_wsl_con_mock(monkeypatch):
    llamadas = []

    def falso_run(cmd, capture_output=False, text=False, timeout=None):
        llamadas.append((cmd, capture_output, text, timeout))
        return SimpleNamespace(returncode=0, stdout="active (running)\n", stderr="")

    monkeypatch.setattr(herramientas.subprocess, "run", falso_run)
    salida = herramientas.ejecutar_wsl("systemctl is-active postgresql")
    assert llamadas == [(["wsl", "-e", "bash", "-c", "systemctl is-active postgresql"], True, True, 30)]
    assert "Código de salida 0" in salida
    assert "active (running)" in salida


def test_ejecutar_wsl_bloquea_destructivos(monkeypatch):
    monkeypatch.setattr(
        herramientas.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("No debía ejecutar rm -rf")),
    )
    assert "bloqueado por seguridad" in herramientas.ejecutar_wsl("rm -rf /var/lib/postgresql").lower()
    assert "bloqueado por seguridad" in herramientas.ejecutar_wsl("psql -c 'DROP DATABASE gnuhealth'").lower()


def test_ejecutar_ssh_vm_con_mock_paramiko(monkeypatch):
    conexiones = []
    comandos = []

    class CanalFalso:
        @staticmethod
        def recv_exit_status():
            return 0

    class StreamFalso:
        def __init__(self, datos: bytes):
            self._datos = datos
            self.channel = CanalFalso()

        def read(self):
            return self._datos

    class SSHClientFalso:
        def __init__(self):
            self.politica = None
            self.cerrado = False

        def set_missing_host_key_policy(self, politica):
            self.politica = politica

        def connect(self, hostname=None, port=None, username=None, password=None, timeout=None):
            conexiones.append({
                "hostname": hostname,
                "port": port,
                "username": username,
                "password": password,
                "timeout": timeout,
            })

        def exec_command(self, comando, timeout=None):
            comandos.append((comando, timeout))
            return None, StreamFalso(b"trytond.service active\n"), StreamFalso(b"")

        def close(self):
            self.cerrado = True

    class ParamikoFalso:
        SSHClient = SSHClientFalso

        @staticmethod
        def AutoAddPolicy():
            return "AutoAddPolicy"

    monkeypatch.setattr(herramientas, "paramiko", ParamikoFalso)
    resultado = herramientas.ejecutar_ssh_vm("systemctl status trytond")

    assert len(conexiones) == 1
    assert conexiones[0]["hostname"] == "192.168.100.24"
    assert conexiones[0]["port"] == 22
    assert conexiones[0]["username"] == "gnuhealth"
    assert conexiones[0]["timeout"] == 30
    assert comandos == [("systemctl status trytond", 30)]
    assert "trytond.service active" in resultado


def test_ejecutar_ssh_vm_bloquea_destructivos():
    assert "bloqueado por seguridad" in herramientas.ejecutar_ssh_vm("mkfs.ext4 /dev/sda1").lower()
    assert "bloqueado por seguridad" in herramientas.ejecutar_ssh_vm("dd if=/dev/zero of=/dev/sda").lower()


def test_inspeccionar_pantalla_hd_con_mock(monkeypatch):
    from PIL import Image

    img_falsa = Image.new("RGB", (1920, 1080), color=(40, 40, 40))

    class CapturaFalsa:
        size = (1920, 1080)
        rgb = img_falsa.tobytes()

    class SctFalso:
        primary_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        monitors = [primary_monitor, primary_monitor]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def grab(self, region):
            return CapturaFalsa()

    class MssModuloFalso:
        @staticmethod
        def mss():
            return SctFalso()

    monkeypatch.setattr(herramientas, "mss", MssModuloFalso)
    monkeypatch.setattr(
        herramientas, "_info_ventana_activa",
        lambda: ("DBeaver 24.0 - PostgreSQL@192.168.100.24", "dbeaver.exe", {"left": 0, "top": 0, "width": 1920, "height": 1080}),
    )

    resultado = herramientas.inspeccionar_pantalla_hd()
    assert "1920x1080" in resultado
    assert "PNG" in resultado
    assert "DBeaver 24.0 - PostgreSQL@192.168.100.24" in resultado
    assert {"ejecutar_wsl", "ejecutar_ssh_vm", "inspeccionar_pantalla_hd"} <= vision.NO_BLOQUEANTES
