class BotWebSocket {
  constructor(userId) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.url = `${proto}//${location.host}/ws/${userId}`;
    this.handlers = {};
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 30000;
    this.pingInterval = null;
    this.consecutiveFailures = 0;
    this.connect();
  }

  connect() {
    this.ws = new WebSocket(this.url);
    let opened = false;

    this.ws.onopen = () => {
      opened = true;
      this.consecutiveFailures = 0;
      this.reconnectDelay = 1000;
      this._startPing();
      (this.handlers['_connected'] || []).forEach(fn => fn());
    };

    this.ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'pong') return;
      (this.handlers[msg.type] || []).forEach(fn => fn(msg));
    };

    this.ws.onclose = () => {
      this._stopPing();
      (this.handlers['_disconnected'] || []).forEach(fn => fn());
      if (!opened) {
        this.consecutiveFailures++;
        if (this.consecutiveFailures >= 3) {
          this.consecutiveFailures = 0;
          fetch('/dashboard', { method: 'HEAD' }).then(r => {
            if (r.redirected) {
              window.location.href = '/login';
            } else {
              setTimeout(() => this.connect(), this.reconnectDelay);
            }
          }).catch(() => {
            setTimeout(() => this.connect(), this.reconnectDelay);
          });
          this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
          return;
        }
      }
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    };

    this.ws.onerror = () => {};
  }

  on(type, handler) {
    if (!this.handlers[type]) this.handlers[type] = [];
    this.handlers[type].push(handler);
    return this;
  }

  _startPing() {
    this._stopPing();
    this.pingInterval = setInterval(() => {
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 30000);
  }

  _stopPing() {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }
}
