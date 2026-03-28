from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import psycopg2.errors
import pdfplumber
import requests
import os
import re
import random

load_dotenv()

app = Flask(__name__)
# Vercel and Cloud Run both have read-only filesystems except /tmp
UPLOAD_FOLDER = (
    '/tmp/uploads'
    if os.environ.get('VERCEL') or os.environ.get('K_SERVICE')
    else os.path.join(os.path.dirname(__file__), 'uploads')
)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Cloud Run connects to Cloud SQL via Unix socket (Auth Proxy built-in).
# Set INSTANCE_CONNECTION_NAME env var on Cloud Run (e.g. project:region:instance).
_instance_conn = os.getenv('INSTANCE_CONNECTION_NAME')
DB_CONFIG = {
    'dbname':   os.getenv('PG_DB', 'vocabquiz'),
    'user':     os.getenv('PG_USER', 'postgres'),
    'password': os.getenv('PG_PASSWORD', ''),
}
if _instance_conn:
    DB_CONFIG['host'] = f'/cloudsql/{_instance_conn}'
else:
    DB_CONFIG['host'] = os.getenv('PG_HOST', 'localhost')
    DB_CONFIG['port'] = os.getenv('PG_PORT', '5432')

STOPWORDS = set([
    'the','be','to','of','and','in','that','have','it','for','not','on','with',
    'as','you','do','at','this','but','his','by','from','they','we','say','her',
    'she','or','an','will','my','one','all','would','there','their','what','so',
    'up','out','if','about','who','get','which','go','me','when','make','can',
    'like','time','no','just','him','know','take','into','your','good','some',
    'could','them','see','other','than','then','now','look','only','come','its',
    'over','think','also','back','after','use','two','how','our','work','first',
    'well','way','even','new','want','because','any','these','give','day','most',
    'us','been','was','were','are','had','has','did','said','each','which','she',
    'more','very','many','much','such','great','little','own','old','right','big',
    'high','different','small','large','next','early','long','young','still',
    'might','should','where','both','those','off','always','never','here','those',
    'come','their','them','these','came','went','know','made','year','hand','part',
    'place','case','week','company','system','program','question','government',
    'number','night','point','home','water','room','mother','area','money','story',
    'fact','month','lot','right','study','book','eye','job','word','though','away',
    'turn','move','live','head','stand','own','page','should','country','found',
    'answer','school','grow','plant','cover','food','state','keep','children',
    'feet','land','side','without','once','something','real','life','few','north',
    'open','seem','together','next','white','begin','got','walk','example','ease',
    'paper','group','always','music','those','both','mark','letter','until','mile',
    'river','feet','care','second','enough','plain','girl','usual','young','ready',
])


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    conn = get_conn()
    with conn.cursor() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS words (
                id             SERIAL PRIMARY KEY,
                word           TEXT UNIQUE NOT NULL,
                definition     TEXT NOT NULL DEFAULT '',
                part_of_speech TEXT NOT NULL DEFAULT '',
                example        TEXT NOT NULL DEFAULT '',
                vietnamese     TEXT NOT NULL DEFAULT '',
                source_file    TEXT NOT NULL DEFAULT '',
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                known          BOOLEAN NOT NULL DEFAULT FALSE
            )
        ''')
        # Add vietnamese column to existing tables that don't have it yet
        c.execute("""
            ALTER TABLE words ADD COLUMN IF NOT EXISTS
            vietnamese TEXT NOT NULL DEFAULT ''
        """)
    conn.commit()
    conn.close()


def get_definition(word):
    try:
        r = requests.get(
            f'https://api.dictionaryapi.dev/api/v2/entries/en/{word}',
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            meanings = data[0].get('meanings', [])
            if meanings:
                m = meanings[0]
                defs = m.get('definitions', [])
                if defs:
                    return {
                        'definition': defs[0].get('definition', ''),
                        'part_of_speech': m.get('partOfSpeech', ''),
                        'example': defs[0].get('example', ''),
                        'found': True
                    }
    except Exception:
        pass
    return {'definition': '', 'part_of_speech': '', 'example': '', 'found': False}


_VI_CHARS = re.compile(r'[àáâãèéêìíòóôõùúýăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỷỹỵ'
                       r'ÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯẠẢẤẦẨẪẬẮẰẲẴẶẸẺẼẾỀỂỄỆỈỊỌỎỐỒỔỖỘỚỜỞỠỢỤỦỨỪỬỮỰỲỶỸỴ]')

def get_vietnamese(word, definition=''):
    """Translate word to Vietnamese using MyMemory free API.
    Uses the definition as context for more accurate results."""
    # Translate the definition for context; fall back to the bare word
    query = definition.strip() if definition.strip() else word
    try:
        r = requests.get(
            'https://api.mymemory.translated.net/get',
            params={'q': query, 'langpair': 'en|vi'},
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            text = data.get('responseData', {}).get('translatedText', '').strip()
            # Must differ from input AND contain actual Vietnamese characters
            if text and text.lower() != query.lower() and _VI_CHARS.search(text):
                return text
    except Exception:
        pass
    return ''


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/upload-pdf', methods=['POST'])
def upload_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'File must be a PDF'}), 400

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    text = ''
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                pt = page.extract_text()
                if pt:
                    text += pt + ' '
    except Exception as e:
        return jsonify({'error': f'Failed to read PDF: {e}'}), 500

    words = re.findall(r'\b[a-zA-Z]+\b', text)
    words = [w.lower() for w in words if len(w) >= 5 and w.lower() not in STOPWORDS]
    seen = set()
    unique_words = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique_words.append(w)

    conn = get_conn()
    with conn.cursor() as c:
        c.execute('SELECT word FROM words')
        existing = set(r[0] for r in c.fetchall())
    conn.close()

    new_words = [w for w in unique_words if w not in existing]
    already_saved = len(unique_words) - len(new_words)

    # Apply word limit if specified
    limit = request.form.get('limit', 0, type=int)
    if limit > 0:
        new_words = new_words[:limit]

    return jsonify({
        'filename': file.filename,
        'new_words': new_words,
        'already_saved': already_saved,
        'total_extracted': len(unique_words)
    })


@app.route('/api/word-definition', methods=['POST'])
def word_definition():
    word = request.json.get('word', '').strip().lower()
    if not word:
        return jsonify({'error': 'No word provided'}), 400
    result = get_definition(word)
    result['vietnamese'] = get_vietnamese(word, result.get('definition', ''))
    return jsonify({'word': word, **result})


@app.route('/api/vocabulary', methods=['GET'])
def get_vocabulary():
    conn = get_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute('''
            SELECT id, word, definition, part_of_speech, example,
                   vietnamese, source_file, created_at, known
            FROM words
            ORDER BY created_at DESC
        ''')
        words = [dict(r) for r in c.fetchall()]
    conn.close()
    for w in words:
        w['created_at'] = w['created_at'].isoformat() if w['created_at'] else None
        w['known'] = bool(w['known'])
    return jsonify(words)


@app.route('/api/vocabulary', methods=['POST'])
def add_word():
    data = request.json
    word = data.get('word', '').strip().lower()
    if not word:
        return jsonify({'error': 'No word provided'}), 400

    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute(
                '''INSERT INTO words (word, definition, part_of_speech, example, vietnamese, source_file)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
                (word, data.get('definition', ''), data.get('part_of_speech', ''),
                 data.get('example', ''), data.get('vietnamese', ''), data.get('source_file', ''))
            )
            wid = c.fetchone()[0]
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Word already exists'}), 409
    finally:
        conn.close()

    return jsonify({'id': wid, 'word': word})


@app.route('/api/vocabulary/batch', methods=['POST'])
def add_words_batch():
    data = request.json
    words_list = data.get('words', [])
    source = data.get('source_file', '')

    conn = get_conn()
    added = 0
    with conn.cursor() as c:
        for wd in words_list:
            try:
                c.execute(
                    '''INSERT INTO words (word, definition, part_of_speech, example, vietnamese, source_file)
                       VALUES (%s, %s, %s, %s, %s, %s)''',
                    (wd['word'], wd.get('definition', ''), wd.get('part_of_speech', ''),
                     wd.get('example', ''), wd.get('vietnamese', ''), source)
                )
                added += 1
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
    conn.commit()
    conn.close()
    return jsonify({'added': added})


@app.route('/api/vocabulary/<int:wid>', methods=['DELETE'])
def delete_word(wid):
    conn = get_conn()
    with conn.cursor() as c:
        c.execute('DELETE FROM words WHERE id = %s', (wid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/vocabulary/<int:wid>/known', methods=['PATCH'])
def toggle_known(wid):
    conn = get_conn()
    with conn.cursor() as c:
        c.execute('UPDATE words SET known = NOT known WHERE id = %s', (wid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/quiz', methods=['GET'])
def generate_quiz():
    count = min(int(request.args.get('count', 10)), 50)
    unknown_only = request.args.get('unknown_only', 'false') == 'true'
    lang = request.args.get('lang', 'en')  # 'en' or 'vi'

    conn = get_conn()
    with conn.cursor() as c:
        if lang == 'vi':
            query = "SELECT id, word, vietnamese FROM words WHERE vietnamese != '' AND vietnamese IS NOT NULL"
        else:
            query = "SELECT id, word, definition FROM words WHERE definition != '' AND definition IS NOT NULL"
        if unknown_only:
            query += " AND known = FALSE"
        c.execute(query)
        all_words = c.fetchall()
    conn.close()

    if len(all_words) < 4:
        suffix = " (unknown words)" if unknown_only else ""
        lang_label = "Vietnamese meanings" if lang == 'vi' else "definitions"
        return jsonify({'error': f'Need at least 4 words with {lang_label}{suffix} to generate a quiz'}), 400

    quiz_words = random.sample(all_words, min(count, len(all_words)))
    questions = []
    for q_id, q_word, q_answer in quiz_words:
        wrong_pool = [w for w in all_words if w[0] != q_id]
        wrong = random.sample(wrong_pool, min(3, len(wrong_pool)))
        choices = [q_answer] + [w[2] for w in wrong]
        random.shuffle(choices)
        questions.append({'id': q_id, 'word': q_word, 'correct': q_answer, 'choices': choices})

    return jsonify(questions)


init_db()

if __name__ == '__main__':
    app.run(debug=True, port=8000)
