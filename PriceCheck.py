"""
app.py — Sukses Crown Toys · Cek Harga
======================================
Keamanan:
  - Semua input user divalidasi dan di-sanitasi sebelum digunakan
  - Seluruh query menggunakan parameterized query (?) — TIDAK ada string
    interpolation untuk data user
  - Integer offset/limit di-cast dan di-clamp secara eksplisit (tidak
    pernah masuk ke query sebagai string user)
  - Kode barang di-whitelist karakter (hanya A-Z, 0-9, strip, titik)
  - Error detail database TIDAK dikirim ke client (hanya pesan generic)
  - DEBUG selalu False di production

Kompatibilitas SQL Server:
  - Menggunakan ROW_NUMBER() agar kompatibel dengan SQL Server 2005+
  - TIDAK menggunakan OFFSET...FETCH (butuh SQL Server 2012+)
"""

from flask import Flask, render_template, jsonify, request
import pyodbc
import os
import re
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Konfigurasi DB ────────────────────────────────────────────────────────────
DB_CONFIG = {
    'server':   os.getenv('DB_SERVER'),
    'database': os.getenv('DB_DATABASE', 'SOLID_SIM'),
    'username': os.getenv('DB_USERNAME'),
    'password': os.getenv('DB_PASSWORD'),
    'driver':   os.getenv('DB_DRIVER', 'ODBC Driver 17 for SQL Server'),
}

_required = ['server', 'username', 'password']
_missing  = [k for k in _required if not DB_CONFIG.get(k)]
if _missing:
    logger.error("Konfigurasi .env belum lengkap: %s", ', '.join(k.upper() for k in _missing))
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db_connection():
    """Buka koneksi baru ke SQL Server."""
    conn_str = (
        f"DRIVER={{{DB_CONFIG['driver']}}};"
        f"SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['username']};"
        f"PWD={DB_CONFIG['password']};"
        "TrustServerCertificate=yes;"
        "Connection Timeout=10;"
    )
    return pyodbc.connect(conn_str)


def _safe_kode(raw):
    """
    Validasi & normalisasi kode barang / kode customer.
    Hanya izinkan: huruf, angka, strip, titik, garis bawah, slash, spasi.
    Kembalikan None jika tidak valid.
    """
    if not raw:
        return None
    cleaned = raw.strip().upper()
    # Whitelist: A-Z 0-9 - . _ / spasi (max 50 karakter)
    if not re.match(r'^[A-Z0-9\-\._/ ]{1,50}$', cleaned):
        return None
    return cleaned


def _safe_int(value, default, min_val, max_val):
    """Cast ke int dengan clamp; tidak pernah raise."""
    try:
        return max(min_val, min(max_val, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_search(raw, max_len=100):
    """
    Sanitasi keyword pencarian untuk LIKE prefix (input%).
    - Potong panjang maksimum
    - Escape karakter wildcard SQL (%, _, [) agar tidak bisa dimanipulasi
    - Wildcard % hanya ditambahkan di AKHIR (prefix search), bukan di awal
    - String ini TETAP dimasukkan lewat parameterized query (?)
    """
    if not raw:
        return ''
    s = raw.strip()[:max_len]
    # Escape wildcard SQL Server agar input user tidak bisa inject wildcard
    s = s.replace('[', '[[]').replace('%', '[%]').replace('_', '[_]')
    return s


def _mask_phone(phone: str) -> str:
    """
    Sensor nomor telepon/HP — tampilkan hanya 4 digit terakhir.
    Contoh: 08123456789 -> ****-****-6789
    """
    if not phone:
        return ''
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) <= 4:
        return phone  # terlalu pendek, kembalikan apa adanya (mungkin bukan nomor)
    masked_len = len(digits) - 4
    return '*' * masked_len + digits[-4:]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── API: Cek Harga by Kode ────────────────────────────────────────────────────

@app.route('/api/cek-harga/<path:kode_raw>')
def cek_harga(kode_raw):
    kode = _safe_kode(kode_raw)
    if not kode:
        return jsonify({'success': False, 'message': 'Kode tidak valid'}), 400

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # Parameterized — AMAN dari SQL Injection
        cursor.execute(
            """
            SELECT [Supplier], [Kategori], [Barang], [Kode], [Isi], [Harga Jual]
            FROM   [dbo].[mon_m_barang_daftar_harga]
            WHERE  [Kode] = ?
            """,
            (kode,)
        )
        row = cursor.fetchone()

        if row:
            return jsonify({
                'success': True,
                'data': {
                    'supplier':   row[0] or '',
                    'kategori':   row[1] or '',
                    'barang':     row[2] or '',
                    'kode':       row[3] or '',
                    'isi':        row[4] or '',
                    'harga_jual': float(row[5]) if row[5] is not None else 0.0,
                }
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Barang dengan kode "{}" tidak ditemukan'.format(kode),
            }), 404

    except pyodbc.Error:
        logger.exception("DB error on cek_harga kode=%s", kode)
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on cek_harga kode=%s", kode)
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── API: List / Search Barang ─────────────────────────────────────────────────

@app.route('/api/barang')
def list_barang():
    """
    Query params (semua divalidasi):
      q        -- keyword pencarian (nama / kode) — prefix search: 'q%'
      kategori -- filter kategori (exact match)
      page     -- halaman (min 1)
      limit    -- item per halaman (10-100, default 30)

    ATURAN:
    - Tidak mengembalikan data jika q < 2 karakter DAN kategori kosong
    - Pencarian menggunakan PREFIX: 'input%' (bukan '%input%')
    - Kategori list selalu dikembalikan untuk isi dropdown
    """
    raw_q        = request.args.get('q', '')
    raw_kategori = request.args.get('kategori', '')
    page         = _safe_int(request.args.get('page',  1),  default=1,  min_val=1,  max_val=9999)
    limit        = _safe_int(request.args.get('limit', 30), default=30, min_val=10, max_val=100)

    q        = _safe_search(raw_q)
    kategori = re.sub(r'[^\w\s\-]', '', raw_kategori.strip())[:60]

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # ── Selalu ambil kategori list untuk dropdown ──────────────────────
        cursor.execute(
            """
            SELECT DISTINCT [Kategori]
            FROM   [dbo].[mon_m_barang_daftar_harga]
            WHERE  [Kategori] IS NOT NULL
              AND  LEN(LTRIM(RTRIM([Kategori]))) > 0
            ORDER  BY [Kategori]
            """
        )
        kategori_list = [r[0] for r in cursor.fetchall() if r[0]]

        # ── Wajib ada keyword min 2 karakter atau pilih kategori ───────────
        if len(q) < 2 and not kategori:
            return jsonify({
                'success':        True,
                'total':          0,
                'page':           1,
                'limit':          limit,
                'pages':          1,
                'kategori_list':  kategori_list,
                'data':           [],
                'require_search': True,
                'message':        'Ketik minimal 2 karakter atau pilih kategori',
            })

        # ── Build WHERE (PREFIX search: q%) ───────────────────────────────
        conditions   = []
        params_count = []
        params_data  = []

        if q:
            # PREFIX ONLY: 'input%'  — tidak ada leading %
            like_q = '{}%'.format(q)
            conditions.append("([Barang] LIKE ? ESCAPE '[' OR [Kode] LIKE ? ESCAPE '[')")
            params_count += [like_q, like_q]
            params_data  += [like_q, like_q]

        if kategori:
            conditions.append("[Kategori] = ?")
            params_count.append(kategori)
            params_data.append(kategori)

        where = 'WHERE ' + ' AND '.join(conditions)

        # ── COUNT — hitung unique Kode saja ────────────────────────────────
        # Pakai COUNT DISTINCT agar sesuai dengan data yang ditampilkan
        cursor.execute(
            "SELECT COUNT(DISTINCT [Kode]) FROM [dbo].[mon_m_barang_daftar_harga] {}".format(where),
            params_count
        )
        total = cursor.fetchone()[0]
        pages = max(1, (total + limit - 1) // limit)
        page  = min(page, pages)

        row_start = (page - 1) * limit + 1
        row_end   = page * limit

        # ── DATA — deduplikasi per Kode, lalu paginasi ─────────────────────
        # Level 1 (_dedup): ambil 1 baris per Kode (prioritas Harga Jual DESC)
        # Level 2 (_paged): beri nomor urut setelah dedup, lalu potong halaman
        # Hasilnya: tidak ada kode ganda di tampilan, pagination tetap akurat
        data_query = """
            SELECT [Supplier], [Kategori], [Barang], [Kode], [Isi], [Harga Jual]
            FROM (
                SELECT
                    [Supplier], [Kategori], [Barang], [Kode], [Isi], [Harga Jual],
                    ROW_NUMBER() OVER (ORDER BY [Barang], [Kode]) AS _rn
                FROM (
                    SELECT
                        [Supplier], [Kategori], [Barang], [Kode], [Isi], [Harga Jual],
                        ROW_NUMBER() OVER (
                            PARTITION BY [Kode]
                            ORDER BY [Harga Jual] DESC
                        ) AS _dedup
                    FROM [dbo].[mon_m_barang_daftar_harga]
                    {where}
                ) AS _d
                WHERE _dedup = 1
            ) AS _p
            WHERE _rn BETWEEN {rs} AND {re}
            ORDER BY _rn
        """.format(where=where, rs=row_start, re=row_end)

        cursor.execute(data_query, params_data)
        rows = cursor.fetchall()

        return jsonify({
            'success':       True,
            'total':         total,
            'page':          page,
            'limit':         limit,
            'pages':         pages,
            'kategori_list': kategori_list,
            'data': [
                {
                    'supplier':   r[0] or '',
                    'kategori':   r[1] or '',
                    'barang':     r[2] or '',
                    'kode':       r[3] or '',
                    'isi':        r[4] or '',
                    'harga_jual': float(r[5]) if r[5] is not None else 0.0,
                }
                for r in rows
            ]
        })

    except pyodbc.Error:
        logger.exception("DB error on list_barang")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on list_barang")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── API: List / Search Member ─────────────────────────────────────────────────

@app.route('/api/member')
def list_member():
    """
    Query params (semua divalidasi):
      q     -- keyword prefix (nama / kota / kode) — 'q%'
      page  -- halaman (min 1)
      limit -- item per halaman (10-100, default 30)

    ATURAN:
    - Hanya Status = 1
    - Tidak mengembalikan data jika q < 2 karakter (harus cari dulu)
    - Pencarian PREFIX: 'q%'
    - Nomor HP & Telepon di-sensor: hanya 4 digit terakhir yang tampil
    """
    raw_q = request.args.get('q', '')
    page  = _safe_int(request.args.get('page',  1),  default=1,  min_val=1,  max_val=9999)
    limit = _safe_int(request.args.get('limit', 30), default=30, min_val=10, max_val=100)

    q = _safe_search(raw_q)

    # Wajib keyword minimal 2 karakter
    if len(q) < 2:
        return jsonify({
            'success':        True,
            'total':          0,
            'page':           1,
            'limit':          limit,
            'pages':          1,
            'data':           [],
            'require_search': True,
            'message':        'Ketik minimal 2 karakter nama / kota / kode member',
        })

    conn = None
    try:
        conn   = get_db_connection()
        cursor = conn.cursor()

        # Status = 1 selalu hardcoded — bukan dari input user
        # PREFIX search: 'q%' pada Customer, Kota, Kode
        # (Telepon/HP tidak bisa di-search dari frontend karena disensor)
        like_q = '{}%'.format(q)
        conditions = [
            "[Status] = 1",
            "([Customer] LIKE ? ESCAPE '[' OR [Kota] LIKE ? ESCAPE '[' OR [Kode] LIKE ? ESCAPE '[')"
        ]
        params_count = [like_q, like_q, like_q]
        params_data  = [like_q, like_q, like_q]

        where = 'WHERE ' + ' AND '.join(conditions)

        # ── COUNT ──────────────────────────────────────────────────────────
        cursor.execute(
            "SELECT COUNT(*) FROM [dbo].[mon_m_customer] {}".format(where),
            params_count
        )
        total = cursor.fetchone()[0]
        pages = max(1, (total + limit - 1) // limit)
        page  = min(page, pages)

        row_start = (page - 1) * limit + 1
        row_end   = page * limit

        # ── DATA (ROW_NUMBER) ──────────────────────────────────────────────
        data_query = """
            SELECT * FROM (
                SELECT
                    [Kode], [Customer], [Kota], [Alamat],
                    [Telepon], [Fax], [HP],
                    [Point], [Disc], [Status], [Keterangan],
                    ROW_NUMBER() OVER (ORDER BY [Customer]) AS _rn
                FROM [dbo].[mon_m_customer]
                {where}
            ) AS _paged
            WHERE _rn BETWEEN {rs} AND {re}
            ORDER BY _rn
        """.format(where=where, rs=row_start, re=row_end)

        cursor.execute(data_query, params_data)
        rows = cursor.fetchall()

        return jsonify({
            'success': True,
            'total':   total,
            'page':    page,
            'limit':   limit,
            'pages':   pages,
            'data': [
                {
                    'kode':       r[0] or '',
                    'customer':   r[1] or '',
                    'kota':       r[2] or '',
                    'alamat':     r[3] or '',
                    # Sensor: hanya 4 digit terakhir yang tampil
                    'telepon':    _mask_phone(r[4] or ''),
                    'fax':        _mask_phone(r[5] or ''),
                    'hp':         _mask_phone(r[6] or ''),
                    'point':      float(r[7]) if r[7] is not None else 0.0,
                    'disc':       float(r[8]) if r[8] is not None else 0.0,
                    'status':     r[9],
                    'keterangan': r[10] or '',
                }
                for r in rows
            ]
        })

    except pyodbc.Error:
        logger.exception("DB error on list_member")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan database'}), 500
    except Exception:
        logger.exception("Unexpected error on list_member")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan server'}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = _safe_int(os.getenv('FLASK_PORT', 5000), default=5000, min_val=1024, max_val=65535)
    logger.info("Starting Sukses Crown Toys server on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)