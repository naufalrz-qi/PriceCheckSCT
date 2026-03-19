from flask import Flask, render_template, jsonify
import pyodbc
import os
import sys
from dotenv import load_dotenv

# Load variabel dari file .env
load_dotenv()

app = Flask(__name__)

# =============================================
# KONFIGURASI DATABASE (dibaca dari file .env)
# =============================================
DB_CONFIG = {
    'server':   os.getenv('DB_SERVER'),
    'database': os.getenv('DB_DATABASE', 'SOLID_SIM'),
    'username': os.getenv('DB_USERNAME'),
    'password': os.getenv('DB_PASSWORD'),
    'driver':   os.getenv('DB_DRIVER', 'ODBC Driver 17 for SQL Server'),
}

# Validasi: pastikan semua konfigurasi wajib sudah diisi di .env
_required_keys = ['server', 'username', 'password']
_missing = [k for k in _required_keys if not DB_CONFIG.get(k)]
if _missing:
    print(f"[ERROR] Konfigurasi database belum lengkap di file .env: {', '.join(_missing).upper()}")
    print("        Salin .env.example menjadi .env dan isi nilainya.")
    sys.exit(1)


def get_db_connection():
    conn_str = (
        f"DRIVER={{{DB_CONFIG['driver']}}};"
        f"SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['username']};"
        f"PWD={DB_CONFIG['password']};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/cek-harga/<kode>')
def cek_harga(kode):
    if not kode or len(kode.strip()) == 0:
        return jsonify({'success': False, 'message': 'Kode tidak valid'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        query = """
            SELECT [Supplier], [Kategori], [Barang], [Kode], [Isi], [Harga Jual]
            FROM [dbo].[mon_m_barang_daftar_harga]
            WHERE [Kode] = ?
        """
        cursor.execute(query, (kode.strip().upper(),))
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({
                'success': True,
                'data': {
                    'supplier': row[0],
                    'kategori': row[1],
                    'barang':   row[2],
                    'kode':     row[3],
                    'isi':      row[4],
                    'harga_jual': float(row[5]) if row[5] is not None else 0
                }
            })
        else:
            return jsonify({'success': False, 'message': f'Barang dengan kode "{kode}" tidak ditemukan'}), 404

    except pyodbc.Error as e:
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500


if __name__ == '__main__':
    port = int(os.getenv('FLASK_PORT', 5000))
    # host='0.0.0.0' agar bisa diakses dari jaringan LAN
    app.run(host='0.0.0.0', port=port, debug=False)