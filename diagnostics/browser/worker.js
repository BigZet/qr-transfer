"use strict";
self.onmessage = (message) => {
  if (message.data === "probe") self.postMessage("i00-worker-ok");
};
