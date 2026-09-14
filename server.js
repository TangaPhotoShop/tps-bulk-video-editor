const http = require('http');
const fs = require('fs');
const path = require('path');
const port = process.env.PORT || 3000;
http.createServer((req, res) => {
  const file = req.url === '/health' ? null : path.join(__dirname, 'web', 'index.html');
  if (!file) { res.writeHead(200, {'content-type':'application/json'}); return res.end(JSON.stringify({ok:true, app:'TPS Bulk Video Editor'})); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(500); return res.end('Unable to load page'); }
    res.writeHead(200, {'content-type':'text/html; charset=utf-8'}); res.end(data);
  });
}).listen(port, '0.0.0.0');

