"""O download da Receita cai no meio do corpo; _baixar precisa retomar (Range)."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src import ingestao

CONTEUDO = bytes(range(256)) * 4000  # ~1 MB


def _servidor(cortes: list[int], aceita_range: bool = True):
    """Serve CONTEUDO; cada valor em `cortes` derruba uma resposta após N bytes."""
    pedidos: list[str | None] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencia
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            pedidos.append(rng)
            ini = 0
            if rng and aceita_range:
                ini = int(rng.split("=")[1].rstrip("-"))
                self.send_response(206)
                self.send_header(
                    "Content-Range", f"bytes {ini}-{len(CONTEUDO)-1}/{len(CONTEUDO)}")
            else:
                self.send_response(200)
            corpo = CONTEUDO[ini:]
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            if cortes:
                n = cortes.pop(0)
                self.wfile.write(corpo[:n])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(corpo)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, pedidos


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(ingestao.time, "sleep", lambda s: None)


def test_retoma_com_range_e_arquivo_final_identico(tmp_path):
    srv, pedidos = _servidor(cortes=[300_000, 200_000])
    try:
        destino = tmp_path / "x.zip"
        url = f"http://127.0.0.1:{srv.server_port}/x.zip"
        assert ingestao._baixar(url, destino) == "OK"
    finally:
        srv.shutdown()
    assert destino.read_bytes() == CONTEUDO
    assert not destino.with_suffix(".zip.part").exists()
    # 1ª tentativa sem Range; as demais pedem só o que falta
    assert pedidos[0] is None
    assert pedidos[1] and pedidos[1].startswith("bytes=")
    assert pedidos[2] and pedidos[2].startswith("bytes=")
    ini1 = int(pedidos[1].split("=")[1].rstrip("-"))
    ini2 = int(pedidos[2].split("=")[1].rstrip("-"))
    assert 0 < ini1 < ini2 < len(CONTEUDO)  # progresso só avança


def test_servidor_sem_range_recomeca_do_zero(tmp_path):
    srv, pedidos = _servidor(cortes=[100_000], aceita_range=False)
    try:
        destino = tmp_path / "x.zip"
        url = f"http://127.0.0.1:{srv.server_port}/x.zip"
        assert ingestao._baixar(url, destino) == "OK"
    finally:
        srv.shutdown()
    assert destino.read_bytes() == CONTEUDO  # sem duplicar bytes


def test_falha_mantem_part_para_proxima_execucao(tmp_path):
    srv, _ = _servidor(cortes=[50_000] * 3)
    try:
        destino = tmp_path / "x.zip"
        url = f"http://127.0.0.1:{srv.server_port}/x.zip"
        assert ingestao._baixar(url, destino, tentativas=3) == "FALHOU"
        part = destino.with_suffix(".zip.part")
        assert part.exists() and 0 < part.stat().st_size <= 150_000
        # nova execução (servidor já estável) completa a partir do .part
        assert ingestao._baixar(url, destino, tentativas=3) == "OK"
    finally:
        srv.shutdown()
    assert destino.read_bytes() == CONTEUDO


def test_404_retorna_not_found(tmp_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/nao.zip"
        assert ingestao._baixar(url, tmp_path / "nao.zip") == "NOT_FOUND"
    finally:
        srv.shutdown()
