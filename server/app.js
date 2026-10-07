import express from 'express';
import path from 'path';
import logger from 'morgan';
import bodyParser from 'body-parser';

import indexRouter from './routes/index.js';
import wopiRouter from './routes/wopi.js';

// Maximum request body size handled by the bodyParser package, which bounds
// the document a save (PutFile) may carry. A save that is refused makes
// coolwsd switch every session on that document to read-only, so this has to
// be above the biggest document in the corpus, the 10 MB report with room.
const maxDocumentSize = '200mb';

let app = express();
export default app;

app.use(logger('dev'));
app.use(express.json());
app.use(express.urlencoded({ extended: false }));
app.use(bodyParser.raw({limit: maxDocumentSize}));
let static_dir = new URL('../html', import.meta.url);
app.use(express.static(static_dir.pathname));

app.use('/', indexRouter);
app.use('/wopi', wopiRouter);
