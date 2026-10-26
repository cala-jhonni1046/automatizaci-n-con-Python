"""Atajos que antes se salteaban la lista blanca.  Correr con:  python -m pytest"""
import os
from types import SimpleNamespace

import pytest

from jarvis import herramientas, seguridad

SALTEOS = [
    "time rm -rf ~/x",            # prefijo seguro "time" delante de algo peligroso
    "ls -la\nrm -rf ~/x",         # salto de línea: con shell=True corren las dos líneas
    "ls -la\rrm -rf ~/x",         # retorno de carro
    "git branch -D main",         # borra una rama
    "git diff --output=main.py",  # pisa un archivo
    "git log -o salida.txt",      # -o seguido de un archivo
    "ipconfig /release",          # corta la red
    "ipconfig /renew",
    "date -s 2020-01-01",         # cambia la fecha del sistema
    "cat ~/.ssh/id_rsa",          # secretos que irían al proveedor de IA
    "cat .env",
    "type .env",
    "cat ~/.aws/credentials",
    "cat servidor.pem",
    "echo %GEMINI_API_KEY%",      # variables de entorno: mandarían la clave a la IA
    "echo $GEMINI_API_KEY",
    "echo ${GEMINI_API_KEY}",
    "cat $HOME/notas.txt",
]

# "date" ya no está en la lista blanca: en Windows cambia la fecha o se queda esperando.
SIN_LISTA_BLANCA = ["date", "date 01-01-2026", "date /t", "time", "time 10:00"]

ENVUELTOS = ["time ls", "nohup ls", "xargs ls", "env ls", "sudo ls", "doas ls",
             "watch ls", "timeout 5 ls"]


@pytest.mark.parametrize("comando", SALTEOS + ENVUELTOS + SIN_LISTA_BLANCA)
def test_no_pasa_como_seguro(comando):
    assert not seguridad.es_seguro(comando)


@pytest.mark.parametrize("comando", SALTEOS)
def test_es_peligroso(comando):
    assert seguridad.es_peligroso(comando)


@pytest.mark.parametrize("comando", SALTEOS + ENVUELTOS + SIN_LISTA_BLANCA)
def test_pide_confirmacion_y_no_ejecuta(comando):
    preguntas = []
    seguridad.registrar_confirmador(lambda pregunta: preguntas.append(pregunta) or False)
    resultado = herramientas.ejecutar("ejecutar_comando", {"comando": comando})
    assert len(preguntas) == 1
    assert "NO autorizó" in resultado


@pytest.mark.parametrize("comando", SALTEOS)
def test_peligroso_avisa_con_advertencia(comando):
    preguntas = []
    seguridad.registrar_confirmador(lambda pregunta: preguntas.append(pregunta) or False)
    herramientas.ejecutar("ejecutar_comando", {"comando": comando})
    assert preguntas[0].startswith("⚠️ COMANDO PELIGROSO")


@pytest.mark.parametrize("comando", ["ls -la", "git status", "git branch", "git branch -a",
                                     "git diff", "ipconfig", "cat README.md",
                                     "git log --oneline", "dir",
                                     "dir C:\\Users\\jhonn\\Downloads",  # la barra invertida sigue andando
                                     "echo 100% listo"])                   # un % suelto no es una variable
def test_lo_que_solo_lee_sigue_siendo_seguro(comando):
    assert seguridad.es_seguro(comando)


# ---------------------------------------------------------------- sin shell para la lista blanca

@pytest.fixture
def llamadas(monkeypatch):
    """Reemplaza subprocess.run para ver cómo se ejecutaría el comando, sin ejecutarlo."""
    vistas = []

    def falso_run(argumentos, **kwargs):
        vistas.append((argumentos, kwargs["shell"]))
        return SimpleNamespace(stdout="ok", stderr="", returncode=0)

    monkeypatch.setattr(herramientas.subprocess, "run", falso_run)
    monkeypatch.setattr(herramientas.shutil, "which", lambda programa: None)
    seguridad.registrar_confirmador(lambda pregunta: True)
    return vistas


@pytest.mark.parametrize("comando, esperado", [
    ("ipconfig", "ipconfig"),                                    # programa: línea tal cual
    ("git status", "git status"),
    ("dir C:\\Users", "cmd /d /v:off /c dir C:\\Users"),         # internos de cmd
    ("echo hola", "cmd /d /v:off /c echo hola"),
    ('type "mis notas.txt"', 'cmd /d /v:off /c type "mis notas.txt"'),
    ("cd", "cmd /d /v:off /c cd"),
])
def test_windows_lista_blanca_sin_shell(monkeypatch, llamadas, comando, esperado):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    herramientas.ejecutar("ejecutar_comando", {"comando": comando})
    assert llamadas == [(esperado, False)]


def test_windows_programa_bat_o_cmd_va_por_cmd(monkeypatch, llamadas):
    # npm en Windows es npm.cmd: CreateProcess no lo corre sin cmd.
    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    monkeypatch.setattr(herramientas.shutil, "which", lambda programa: r"C:\nodejs\npm.CMD")
    herramientas.ejecutar("ejecutar_comando", {"comando": "npm --version"})
    assert llamadas == [("cmd /d /v:off /c npm --version", False)]


def test_linux_lista_blanca_sin_shell(monkeypatch, llamadas):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", False)
    herramientas.ejecutar("ejecutar_comando", {"comando": "ls -la ~/proyectos 'con espacio'"})
    assert llamadas == [(["ls", "-la", os.path.expanduser("~/proyectos"), "con espacio"], False)]


def test_wsl_lista_blanca_sin_shell(monkeypatch, llamadas):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    herramientas.ejecutar("ejecutar_comando", {"comando": "ls -la", "en_linux": True})
    assert llamadas == [(["wsl", "-e", "ls", "-la"], False)]


def test_comillas_sin_cerrar_no_ejecuta(monkeypatch, llamadas):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", False)
    resultado = herramientas.ejecutar("ejecutar_comando", {"comando": "echo 'hola"})
    assert "No pude interpretar" in resultado
    assert llamadas == []


@pytest.mark.parametrize("windows", [True, False])
def test_confirmado_va_con_shell(monkeypatch, llamadas, windows):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", windows)
    herramientas.ejecutar("ejecutar_comando", {"comando": "pip install fastapi"})
    assert llamadas == [("pip install fastapi", True)]


def test_confirmado_en_wsl_va_con_bash(monkeypatch, llamadas):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    herramientas.ejecutar("ejecutar_comando", {"comando": "pip install fastapi", "en_linux": True})
    assert llamadas == [(["wsl", "bash", "-lc", "pip install fastapi"], False)]


def test_lista_blanca_corre_de_verdad_sin_shell():
    # Sin mocks: en Windows va por cmd /c (echo es interno), en Linux por /bin/echo.
    resultado = herramientas.ejecutar("ejecutar_comando", {"comando": "echo hola sin shell"})
    assert "Código de salida 0" in resultado
    assert "hola sin shell" in resultado
