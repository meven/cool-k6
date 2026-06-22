import { WebSocket } from 'k6/websockets';
import { check, sleep } from 'k6';

export class CoolClientWs {
    socket;
    bytesSent = 0;
    bytesReceived = 0;

    /**
     * wopiClient: The URL of the WOPI client (the iframe src)
     * wopiSrc: the WOPI Source
     */
    constructor(wopiClient, wopiSrc, onopen) {
        const wssUrl = new URL('/', `${wopiClient}`);
        wssUrl.protocol = "wss:";
        wssUrl.pathname = `cool/${encodeURIComponent(wopiSrc)}/ws`;
        wssUrl.searchParams.set('WOPISrc', wopiSrc)
        wssUrl.searchParams.set('compat', '/ws')

        const start = Date.now();
        this.socket = new WebSocket(wssUrl, null, {
            headers: {
                Origin: `${wopiClient}`
            }
        });
        console.log(`Socket URL: ${this.socket.url}`);
        if (typeof onopen == "function") {
            this.socket.onopen = onopen;
        } else {
            this.socket.onopen = () => {
                console.log("WebSocket: open");
            }
        }
        this.socket.onclose = event => {
            console.log(`WebSocket: close`);
        };
        this.socket.onerror = e => {
            console.error(`WebSocket error: ${e.error}`);
        };
        // k6/websockets in some builds only fires message events
        // through the EventEmitter-style .on('message', ...) pattern;
        // both the onmessage property setter and addEventListener
        // are silently ignored, leaving bytesReceived stuck at 0.
        let messageCount = 0;
        const onMessage = (data) => {
            // .on('message', cb) delivers the data directly (no
            // wrapping MessageEvent), unlike addEventListener.
            messageCount += 1;
            if (typeof data === 'string')
                this.bytesReceived += data.length;
            else if (data && typeof data.byteLength === 'number')
                this.bytesReceived += data.byteLength;
            else if (data && typeof data.size === 'number')
                this.bytesReceived += data.size;
            if (messageCount === 1)
                console.log(`WebSocket: first message via .on, typeof=${typeof data}`);
        };
        if (typeof this.socket.on === 'function') {
            this.socket.on('message', onMessage);
        } else {
            this.socket.addEventListener('message', event => onMessage(event && event.data));
        }
        console.log("Done setup");
    }

    openDocument(wopiSrc) {
        this.send(`load url=${wopiSrc} accessibilityState=false` +
                  ' deviceFormFactor=desktop darkTheme=false timezone=America/Montreal');
    }

    send(data) {
        check(this.socket.readyState, {
            'WebSocket is open': r => r == 1,
        });
        if (typeof data === 'string') this.bytesSent += data.length;
        else if (data && typeof data.byteLength === 'number') this.bytesSent += data.byteLength;
        return this.socket.send(data);
    }

    close() {
        this.socket.close();
    }
}
