const http = require('node:http');
http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({ message: 'Original application is running', version: 'v1' }));
}).listen(4321, '127.0.0.1', () => {
  console.log('Web server started');
});
