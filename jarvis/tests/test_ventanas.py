"""Traer al frente lo que abre Jarvis (Windows), sin abrir ventanas de verdad.  Correr con:  python -m pytest"""
import ctypes
import platform
import struct

import pytest

from jarvis import herramientas

TERMINAL, MUSICA, NAVEGADOR, NUEVA = 100, 200, 300, 400


@pytest.fixture
def windows(monkeypatch):
    """Simula Windows: una lista de ventanas que cambia en cada revisión, y registra a cuál se enfocó."""
    estado = {"secuencia": [], "enfocadas": [], "abierto": False}

    def ventanas():
        secuencia = estado["secuencia"]
        return dict(secuencia.pop(0) if len(secuencia) > 1 else secuencia[0])

    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    monkeypatch.setattr(herramientas, "SEGUNDOS_ESPERANDO_VENTANA", 0.3)
    monkeypatch.setattr(herramientas, "SEGUNDOS_ENTRE_REVISIONES", 0.01)
    monkeypatch.setattr(herramientas, "_ventanas", ventanas)
    monkeypatch.setattr(herramientas, "_ventanas_excluidas", lambda: {TERMINAL})
    monkeypatch.setattr(herramientas, "_programa_de",
                        lambda hwnd: {NAVEGADOR: "chrome.exe", MUSICA: "spotify.exe"}.get(hwnd, ""))
    monkeypatch.setattr(herramientas, "_enfocar", lambda hwnd: estado["enfocadas"].append(hwnd) or True)
    return estado


def abrir(estado):
    return lambda: estado.update(abierto=True)


ANTES = {TERMINAL: "PowerShell", MUSICA: "Canción 1", NAVEGADOR: "Google - Chrome"}


def test_ventana_nueva_pasa_al_frente(windows):
    windows["secuencia"] = [ANTES, ANTES, {**ANTES, NUEVA: "Bloc de notas"}]
    assert herramientas._abrir_al_frente(abrir(windows)) is True
    assert windows["abierto"]
    assert windows["enfocadas"] == [NUEVA]


def test_pestaña_nueva_trae_el_navegador(windows):
    windows["secuencia"] = [ANTES, {**ANTES, NAVEGADOR: "GitHub - Chrome"}]
    assert herramientas._abrir_al_frente(abrir(windows), pestaña_de_navegador=True) is True
    assert windows["enfocadas"] == [NAVEGADOR]


def test_titulo_cambiado_de_otro_programa_no_cuenta(windows):
    # La música cambia de canción mientras tanto: no es lo que abrió Jarvis.
    windows["secuencia"] = [ANTES, {**ANTES, MUSICA: "Canción 2", TERMINAL: "python vision.py"}]
    assert herramientas._abrir_al_frente(abrir(windows), pestaña_de_navegador=True) is False
    assert windows["enfocadas"] == []


def test_fuera_de_la_web_un_titulo_cambiado_no_cuenta(windows):
    windows["secuencia"] = [ANTES, {**ANTES, NAVEGADOR: "GitHub - Chrome"}]
    assert herramientas._abrir_al_frente(abrir(windows)) is False
    assert windows["enfocadas"] == []


def test_si_no_aparece_ventana_avisa_y_no_enfoca_nada(windows):
    windows["secuencia"] = [ANTES]
    assert herramientas._abrir_al_frente(abrir(windows)) is False
    assert windows["abierto"] and windows["enfocadas"] == []


def test_fuera_de_windows_solo_abre(monkeypatch):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", False)
    abiertos = []
    assert herramientas._abrir_al_frente(lambda: abiertos.append(1)) is True
    assert abiertos == [1]


# ---------------------------------------------------------------- las herramientas avisan

@pytest.fixture
def sin_foco(monkeypatch):
    llamadas = []

    def falso(abrir, pestaña_de_navegador=False):
        llamadas.append(pestaña_de_navegador)  # no llama a abrir(): no se abre nada de verdad
        return False

    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    monkeypatch.setattr(herramientas, "_abrir_al_frente", falso)
    return llamadas


def test_las_cuatro_herramientas_avisan_si_quedo_detras(sin_foco, tmp_path):
    archivo = tmp_path / "notas.txt"
    archivo.write_text("hola")
    resultados = [
        herramientas.abrir_web("github.com"),
        herramientas.abrir_programa("bloc de notas"),
        herramientas.abrir_carpeta(str(tmp_path)),
        herramientas.abrir_archivo(str(archivo)),
    ]
    for resultado in resultados:
        assert resultado.startswith("Abrí")
        assert "No pude traer la ventana al frente" in resultado
    assert sin_foco == [True, False, False, False]  # solo la web acepta una pestaña nueva


def test_si_quedo_al_frente_no_avisa(monkeypatch):
    monkeypatch.setattr(herramientas, "ES_WINDOWS", True)
    monkeypatch.setattr(herramientas, "_abrir_al_frente", lambda abrir, pestaña_de_navegador=False: True)
    assert herramientas.abrir_web("github.com") == "Abrí https://github.com."
    assert herramientas.abrir_programa("calculadora") == "Abrí calculadora."


# ---------------------------------------------------------------- Windows de verdad (solo lee)

solo_windows = pytest.mark.skipif(platform.system() != "Windows", reason="usa la API de Windows")


def test_estructura_input_tiene_el_tamaño_de_windows():
    # sizeof(INPUT) es 40 en 64 bits y 28 en 32: si no coincide, SendInput la rechaza.
    assert ctypes.sizeof(herramientas._INPUT) == (40 if struct.calcsize("P") == 8 else 28)


@solo_windows
def test_lee_las_ventanas_reales():
    ventanas = herramientas._ventanas()
    assert isinstance(ventanas, dict)
    assert all(isinstance(h, int) and isinstance(t, str) and t for h, t in ventanas.items())


@solo_windows
def test_sabe_que_programa_es_dueño_de_una_ventana():
    ventanas = herramientas._ventanas()
    if not ventanas:
        pytest.skip("no hay ventanas visibles")
    nombres = [herramientas._programa_de(h) for h in ventanas]
    assert any(n.endswith(".exe") for n in nombres)
