(function() {
  window.getCerebroSocket = window.getCerebroSocket || function() {
    if (!window.io) return null;
    if (!window.cerebroSocket) {
      const options = window.CEREBRO_SOCKET_OPTIONS || {};
      const socket = io({
        transports: options.transports || ["polling", "websocket"],
        upgrade: options.upgrade === undefined ? true : Boolean(options.upgrade),
      });
      window.cerebroSocket = socket;
    }
    return window.cerebroSocket;
  };
})();
