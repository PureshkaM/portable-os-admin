"""Стенд Э14: «гостиничная» страница входа. /accept снимает режим портала."""
import http.server, os
PAGE = """<html><head><meta charset="utf-8"><title>Hotel WiFi</title></head>
<body style="font-family:sans-serif;background:#eef"><h1>Гостиница «Стенд»: Wi-Fi</h1>
<p>Для доступа в интернет примите условия.</p>
<form action="/accept" method="get"><button style="font-size:24px">Принять условия</button></form>
<p><a href="file:///etc/passwd">file-ссылка</a> | <a href="http://10.0.2.2/terms" target="_blank">условия в новом окне</a></p>
</body></html>"""
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/accept"):
            try: os.unlink("/run/poa-stand-portal")
            except FileNotFoundError: pass
            body = "<html><meta charset=utf-8><h1>Готово: интернет открыт</h1></html>"
        else:
            body = PAGE
        data = body.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, fmt, *a): open("/root/gui/fake-portal.log","a").write((fmt % a) + "\n")
http.server.HTTPServer(("127.0.0.1", 8088), H).serve_forever()
