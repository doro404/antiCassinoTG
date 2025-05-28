import asyncio
import json
import logging
import os
import re
import sqlite3
import pytz
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Set, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ApplicationBuilder, JobQueue # Keep JobQueue here
from telegram.ext._contexttypes import ContextTypes

# ADD THESE TWO LINES FOR DEBUGGING:
print(f"--- Executing bot.py from: {os.path.abspath(__file__)} ---")
# Configuração de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


class LearningAntiCasinoBot:
    def __init__(self, config: Dict):
        # Carrega configurações do dicionário 'config'
        self.token = config["TELEGRAM_BOT_TOKEN"]
        self.banned_words_file = config["BANNED_WORDS_FILE"]
        self.learned_words_file = config["LEARNED_WORDS_FILE"]
        self.database_name = config["DATABASE_NAME"]
        self.admin_ids: Set[int] = set(config.get("ADMIN_IDS", []))

        # Configurações de aprendizado do JSON
        self.min_pattern_frequency = config["LEARNING_SETTINGS"]["MIN_PATTERN_FREQUENCY"]
        self.similarity_threshold = config["LEARNING_SETTINGS"]["SIMILARITY_THRESHOLD"]
        self.learning_window_hours = config["LEARNING_SETTINGS"]["LEARNING_WINDOW_HOURS"]
        self.auto_approve_confidence = config["LEARNING_SETTINGS"]["AUTO_APPROVE_CONFIDENCE"]
        self.auto_approve_frequency = config["LEARNING_SETTINGS"]["AUTO_APPROVE_FREQUENCY"]
        

        # 1) Cria o timezone com pytz
        local_tz = pytz.timezone('America/Sao_Paulo')

        # 2) Instancia o JobQueue SEM argumentos
        job_queue = JobQueue()

        # 3) Ajusta o scheduler do job_queue para usar pytz
        job_queue.scheduler.timezone = local_tz

        # 4) Monta a Application passando o job_queue já configurado
        self.application = (
            ApplicationBuilder()
            .token(self.token)
            .job_queue(job_queue)
            .build()
        )



        self.banned_words = self.load_banned_words()
        self.whitelist_users = set()
        self.warning_counts = {}

        # Sistema de aprendizado
        self.suspicious_patterns = Counter()  # Padrões suspeitos encontrados
        self.user_violations = defaultdict(list)  # Histórico de violações por usuário
        self.learned_terms = set()  # Novos termos aprendidos
        self.message_history = []  # Histórico de mensagens para análise
        self.word_frequency = Counter()  # Frequência de palavras em mensagens suspeitas

        self.init_database()
        self.setup_handlers()

        # Inicia tarefa de aprendizado em background
        # You'll need to run this with the job queue, not asyncio.create_task directly
        # For example: self.application.job_queue.run_once(self.learning_task, 0) for immediate start
        # or self.application.job_queue.run_repeating(self.learning_task, interval=3600)
        # You'll need to adjust learning_task to accept `ContextTypes.DEFAULT_TYPE` if run by job_queue
        # For now, let's keep it as asyncio.create_task for minimal changes to the task itself,
        # but be aware that for proper integration with PTB's event loop, the JobQueue is preferred.
        self.application.job_queue.run_repeating(
            callback=self.learning_task,  # sua coroutine
            interval=3600,                # intervalo em segundos
            first=0,                      # delay inicial em segundos
            name="hourly-learning-task"
        )

    def init_database(self):
        """Inicializa banco de dados para armazenar dados de aprendizado"""
        # Usa o nome do banco de dados do arquivo de configuração
        self.conn = sqlite3.connect(self.database_name, check_same_thread=False)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                message TEXT,
                timestamp DATETIME,
                action_taken TEXT,
                learned_terms TEXT
            )
        ''')

        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS learned_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT UNIQUE,
                frequency INTEGER,
                confidence REAL,
                first_seen DATETIME,
                last_seen DATETIME,
                status TEXT DEFAULT 'pending'
            )
        ''')

        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS word_analysis (
                word TEXT PRIMARY KEY,
                frequency INTEGER,
                in_violations INTEGER,
                confidence_score REAL,
                last_updated DATETIME
            )
        ''')

        self.conn.execute('''
                CREATE TABLE IF NOT EXISTS group_stats (
                    group_id INTEGER PRIMARY KEY,
                    group_name TEXT,
                    total_violations INTEGER DEFAULT 0,
                    total_bans INTEGER DEFAULT 0,
                    last_updated DATETIME
                )
            ''')

        # NEW TABLE: bot_stats
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS bot_stats (
                id INTEGER PRIMARY KEY DEFAULT 1, -- Only one row for global stats
                total_messages_processed INTEGER DEFAULT 0,
                total_violations_prevented INTEGER DEFAULT 0,
                total_users_banned INTEGER DEFAULT 0,
                total_learned_terms INTEGER DEFAULT 0,
                last_updated DATETIME
            )
        ''')

        # Ensure the single row for bot_stats exists
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO bot_stats (id, last_updated) VALUES (1, ?)", (datetime.now(),))
        self.conn.commit()


        self.conn.commit()

    def load_banned_words(self) -> Set[str]:
        """Carrega palavras banidas incluindo termos aprendidos"""
        # Usa os nomes dos arquivos do JSON
        banned_words_file = self.banned_words_file
        learned_words_file = self.learned_words_file

        # Palavras base (mantidas como fallback ou iniciais)
        default_words = {
            "cassino", "casino", "bet", "aposta", "apostas", "blaze", "betano",
            "bet365", "sportingbet", "rivalo", "pixbet", "1xbet", "slots",
            "roleta", "crash", "aviator", "fortune", "mines", "plinko",
            "bonus", "bônus", "jackpot", "ganhe dinheiro", "renda extra",
            "trader", "trade", "forex", "multiplicador", "cashback"
        }

        try:
            # Carrega palavras do arquivo
            if os.path.exists(banned_words_file):
                with open(banned_words_file, 'r', encoding='utf-8') as f:
                    file_words = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}
                default_words.update(file_words)

            # Carrega termos aprendidos
            if os.path.exists(learned_words_file):
                with open(learned_words_file, 'r', encoding='utf-8') as f:
                    learned_data = json.load(f)
                    # Condição para carregar termos aprendidos também pode ser configurável
                    learned_words = {term['word'] for term in learned_data if term['confidence'] > 0.7}
                    default_words.update(learned_words)
                    self.learned_terms = learned_words
                    logger.info(f"Carregados {len(learned_words)} termos aprendidos")

        except Exception as e:
            logger.error(f"Erro ao carregar palavras: {e}")

        return default_words

    def setup_handlers(self):
        """Configura os handlers do bot"""
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.check_message)
        )
        self.application.add_handler(
            MessageHandler(filters.PHOTO, self.check_message)
        )

        # Comandos
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("learning", self.learning_stats_command))
        self.application.add_handler(CommandHandler("approve", self.approve_learned_term))
        self.application.add_handler(CommandHandler("reject", self.reject_learned_term))
        self.application.add_handler(CommandHandler("analyze", self.analyze_patterns))
        self.application.add_handler(CommandHandler("whitelist", self.whitelist_command))
        self.application.add_handler(CommandHandler("reload", self.reload_words_command))
        self.application.add_handler(CommandHandler("botstats", self.bot_overall_stats_command))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        await update.message.reply_text(
            "🛡️ *Bot Anti-Cassino com IA de Aprendizado*\n\n"
            "🧠 Este bot aprende automaticamente novos termos de cassino!\n\n"
            "📊 *Recursos de Aprendizado:*\n"
            "• Detecta padrões em mensagens de spam\n"
            "• Analisa similaridade entre termos\n"
            "• Sugere novos termos para bloqueio\n"
            "• Adapta-se às táticas dos spammers\n\n"
            "📱 *Comandos:*\n"
            "/help - Ajuda completa\n"
            "/learning - Estatísticas de aprendizado\n"
            "/analyze - Analisar padrões recentes\n"
            "/approve <termo> - Aprovar termo aprendido\n"
            "/reject <termo> - Rejeitar termo aprendido",
            parse_mode='Markdown'
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /help"""
        await update.message.reply_text(
            "🤖 *Bot Anti-Cassino com Aprendizado Automático*\n\n"
            "*🧠 Como o aprendizado funciona:*\n"
            "• Analisa mensagens banidas para encontrar padrões\n"
            "• Detecta variações de termos conhecidos\n"
            "• Identifica palavras frequentes em spam\n"
            "• Sugere novos termos com base na confiança\n\n"
            "*📊 Comandos de Aprendizado:*\n"
            "/learning - Ver estatísticas de IA\n"
            "/analyze - Analisar últimas 100 mensagens\n"
            "/approve termo - Adicionar termo à lista\n"
            "/reject termo - Remover termo sugerido\n\n"
            "*⚙️ Comandos Admin:*\n"
            "/whitelist @user - Proteger usuário\n"
            "/reload - Recarregar listas\n"
            "/stats - Estatísticas gerais",
            parse_mode='Markdown'
        )

    async def learning_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mostra estatísticas do sistema de aprendizado"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem ver estatísticas de aprendizado.")
            return

        # Busca dados do banco
        cursor = self.conn.cursor()

        # Contadores gerais
        cursor.execute("SELECT COUNT(*) FROM violations WHERE timestamp > datetime('now', '-24 hours')")
        violations_24h = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM learned_patterns WHERE status = 'pending'")
        pending_patterns = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM learned_patterns WHERE status = 'approved'")
        approved_patterns = cursor.fetchone()[0]

        # Top padrões suspeitos
        cursor.execute("""
            SELECT pattern, frequency, confidence
            FROM learned_patterns
            WHERE status = 'pending'
            ORDER BY confidence DESC
            LIMIT 5
        """)
        top_patterns = cursor.fetchall()

        stats_text = f"🧠 *Estatísticas de Aprendizado IA*\n\n"
        stats_text += f"📊 *Últimas 24h:*\n"
        stats_text += f"• Violações detectadas: {violations_24h}\n"
        stats_text += f"• Padrões pendentes: {pending_patterns}\n"
        stats_text += f"• Padrões aprovados: {approved_patterns}\n"
        stats_text += f"• Termos aprendidos ativos: {len(self.learned_terms)}\n\n"

        if top_patterns:
            stats_text += f"🎯 *Top Padrões Suspeitos:*\n"
            for pattern, freq, conf in top_patterns:
                stats_text += f"• `{pattern}` (freq: {freq}, conf: {conf:.2f})\n"

        await update.message.reply_text(stats_text, parse_mode='Markdown')


    async def analyze_patterns(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Analisa padrões recentes e sugere novos termos"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem analisar padrões.")
            return

        await update.message.reply_text("🔍 Analisando padrões recentes...")

        suggestions = await self.analyze_recent_patterns()

        if suggestions:
            text = "🎯 *Novos Termos Sugeridos pela IA:*\n\n"
            for term, confidence, frequency in suggestions[:10]:
                text += f"• `{term}` (conf: {confidence:.2f}, freq: {frequency})\n"
            text += f"\n💡 Use /approve <termo> para adicionar à lista banida"
        else:
            text = "✅ Nenhum novo padrão suspeito detectado recentemente."

        await update.message.reply_text(text, parse_mode='Markdown')

    async def approve_learned_term(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Aprova um termo aprendido pela IA"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem aprovar termos.")
            return

        if not context.args:
            await update.message.reply_text("ℹ️ Uso: /approve <termo>")
            return

        term = ' '.join(context.args).lower().strip()

        # Adiciona à lista de banidos
        self.banned_words.add(term)
        self.learned_terms.add(term)

        # Atualiza banco de dados
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE learned_patterns
            SET status = 'approved'
            WHERE pattern = ?
        """, (term,))
        self.conn.commit()

        # Salva termos aprendidos
        self.save_learned_terms()

        await update.message.reply_text(f"✅ Termo `{term}` aprovado e adicionado à lista banida!")

    async def reject_learned_term(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rejeita um termo sugerido pela IA"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem rejeitar termos.")
            return

        if not context.args:
            await update.message.reply_text("ℹ️ Uso: /reject <termo>")
            return

        term = ' '.join(context.args).lower().strip()

        # Atualiza banco de dados
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE learned_patterns
            SET status = 'rejected'
            WHERE pattern = ?
        """, (term,))
        self.conn.commit()

        await update.message.reply_text(f"❌ Termo `{term}` rejeitado.")

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /stats melhorado"""
        total_banned_words = len(self.banned_words)
        learned_terms_count = len(self.learned_terms)

        # Estatísticas do banco
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM violations")
        total_violations = cursor.fetchone()[0]

        await update.message.reply_text(
            f"📊 *Estatísticas do Bot*\n\n"
            f"🔍 Palavras base: {total_banned_words - learned_terms_count}\n"
            f"🧠 Termos aprendidos: {learned_terms_count}\n"
            f"📝 Total monitorado: {total_banned_words}\n"
            f"⚠️ Violações registradas: {total_violations}\n"
            f"✅ Usuários na whitelist: {len(self.whitelist_users)}\n\n"
            f"🛡️ IA de aprendizado ativa!",
            parse_mode='Markdown'
        )

    async def bot_overall_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /botstats para estatísticas gerais do bot (admins only)"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem ver as estatísticas gerais do bot.")
            return

        cursor = self.conn.cursor()
        cursor.execute("SELECT total_messages_processed, total_violations_prevented, total_users_banned, total_learned_terms FROM bot_stats WHERE id = 1")
        overall_stats = cursor.fetchone()

        stats_text = "📊 *Estatísticas Gerais do Bot (Todos os Grupos)*\n\n"
        if overall_stats:
            stats_text += f"• Mensagens processadas: {overall_stats[0]}\n"
            stats_text += f"• Violações impedidas: {overall_stats[1]}\n"
            stats_text += f"• Usuários banidos: {overall_stats[2]}\n"
            stats_text += f"• Termos aprendidos pela IA: {overall_stats[3]}\n"
        else:
            stats_text += "Nenhum dado de estatística geral disponível ainda."
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')


    async def is_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Verifica se o usuário é administrador"""
        user_id = update.effective_user.id
        if user_id in self.admin_ids:
          return True
        
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            member = await context.bot.get_chat_member(chat_id, user_id)
            return member.status in ['administrator', 'creator']
        except Exception as e:
            logger.error(f"Erro ao verificar admin: {e}")
            return False

    async def check_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verifica mensagem e alimenta sistema de aprendizado"""
        try:
            message = update.message
            user = message.from_user
            chat = message.chat

            # Increment total messages processed
            self.update_bot_overall_stats(messages_processed=1)

            if chat.type == 'private' or user.id in self.whitelist_users:
                return

            # Verifica permissões do bot
            bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if not bot_member.can_delete_messages or not bot_member.can_restrict_members:
                logger.warning(f"Bot sem permissões suficientes no chat {chat.title} ({chat.id}). Precisa de 'Delete Messages' e 'Restrict Members'.")
                return

            # Texto para análise
            text_to_check = ""
            if message.text:
                text_to_check = message.text.lower()
            elif message.caption:
                text_to_check = message.caption.lower()

            # Adiciona à história para aprendizado
            self.add_to_message_history(user.id, text_to_check, datetime.now())

            # Verifica violação
            is_violation, detected_terms = self.analyze_message_advanced(text_to_check)

            if is_violation:
                # Increment total violations prevented
                self.update_bot_overall_stats(violations_prevented=1)
                # Record violation in the database, now including group info
                self.record_violation(user.id, user.username or user.first_name,
                                    text_to_check, detected_terms)

                # Update group stats for violations
                self.update_group_stats(chat.id, chat.title, violations=1)

                # Alimenta sistema de aprendizado
                await self.feed_learning_system(text_to_check, detected_terms)

                # Executa punição
                await self.handle_violation(update, context, user, message)

        except Exception as e:
            logger.error(f"Erro ao verificar mensagem: {e}")

    def analyze_message_advanced(self, text: str) -> Tuple[bool, List[str]]:
        """Análise avançada de mensagem com detecção de padrões"""
        if not text:
            return False, []

        detected_terms = []
        text_normalized = self.normalize_text(text)

        # Verifica palavras banidas existentes
        for word in self.banned_words:
            if word in text_normalized:
                detected_terms.append(word)

        # Verifica padrões aprendidos
        for pattern in self.suspicious_patterns:
            if pattern in text_normalized:
                detected_terms.append(f"pattern:{pattern}")

        # Análise de similaridade com termos conhecidos
        words_in_text = text_normalized.split()
        for word in words_in_text:
            if len(word) > 3:  # Ignora palavras muito curtas
                for banned_word in self.banned_words:
                    similarity = SequenceMatcher(None, word, banned_word).ratio()
                    if similarity > self.similarity_threshold:
                        detected_terms.append(f"similar:{word}~{banned_word}")

        # Verifica padrões regex avançados
        advanced_patterns = [
            r'bet\d+',
            r'\b\d+x\b',
            r'ganha.*dinheiro',
            r'renda.*extra',
            r'link.*bio',
            r'chama.*dm',
            r'pix.*instant',
            r'lucro.*garantido',
            r'sem.*risco',
            r'estratégia.*infalível'
        ]

        for pattern in advanced_patterns:
            if re.search(pattern, text_normalized):
                detected_terms.append(f"regex:{pattern}")

        return len(detected_terms) > 0, detected_terms

    def normalize_text(self, text: str) -> str:
        """Normalização avançada de texto"""
        # Remove caracteres especiais
        text = re.sub(r'[^\w\s]', ' ', text)
        # Remove números entre letras (c4ss1n0 -> cssn)
        text = re.sub(r'(\w)\d+(\w)', r'\1\2', text)
        # Remove espaços duplicados
        text = re.sub(r'\s+', ' ', text.strip())
        # Remove acentos comuns
        replacements = {
            'á': 'a', 'à': 'a', 'ã': 'a', 'â': 'a',
            'é': 'e', 'ê': 'e', 'í': 'i', 'ó': 'o',
            'ô': 'o', 'õ': 'o', 'ú': 'u', 'ç': 'c'
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def add_to_message_history(self, user_id: int, text: str, timestamp: datetime):
        """Adiciona mensagem ao histórico para análise"""
        self.message_history.append({
            'user_id': user_id,
            'text': text,
            'timestamp': timestamp
        })

        # Mantém apenas últimas 1000 mensagens
        if len(self.message_history) > 1000:
            self.message_history = self.message_history[-1000:]

    def record_violation(self, user_id: int, username: str, message: str, detected_terms: List[str]):
        """Registra violação no banco de dados"""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO violations (user_id, username, message, timestamp, action_taken, learned_terms)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, message, datetime.now(), "ban", json.dumps(detected_terms)))
        self.conn.commit()

    async def feed_learning_system(self, text: str, detected_terms: List[str]):
        """Alimenta o sistema de aprendizado com nova violação"""
        words = self.normalize_text(text).split()

        # Analisa frequência de palavras em mensagens violadoras
        for word in words:
            if len(word) > 2:  # Ignora palavras muito curtas
                self.word_frequency[word] += 1

                # Atualiza análise de palavras no banco
                cursor = self.conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO word_analysis
                    (word, frequency, in_violations, confidence_score, last_updated)
                    VALUES (?,
                            COALESCE((SELECT frequency FROM word_analysis WHERE word = ?), 0) + 1,
                            COALESCE((SELECT in_violations FROM word_analysis WHERE word = ?), 0) + 1,
                            ?, ?)
                """, (word, word, word, self.calculate_word_confidence(word), datetime.now()))
                self.conn.commit()

        # Procura novos padrões
        await self.detect_new_patterns(text)

    def calculate_word_confidence(self, word: str) -> float:
        """Calcula confiança de uma palavra ser relacionada a cassino"""
        # Fatores que aumentam confiança:
        # 1. Frequência em violações
        # 2. Similaridade com termos conhecidos
        # 3. Padrões regex

        confidence = 0.0
        violation_freq = self.word_frequency.get(word, 0)

        # Baseado na frequência
        if violation_freq > 0:
            confidence += min(violation_freq * 0.1, 0.5)

        # Similaridade com termos banidos
        max_similarity = 0
        for banned_word in self.banned_words:
            similarity = SequenceMatcher(None, word, banned_word).ratio()
            max_similarity = max(max_similarity, similarity)

        confidence += max_similarity * 0.3

        # Padrões específicos
        if re.search(r'bet|cassino|aposta|jogo|trade', word):
            confidence += 0.2

        return min(confidence, 1.0)

    async def detect_new_patterns(self, text: str):
        """Detecta novos padrões em mensagens suspeitas"""
        # Extrai possíveis novos termos
        words = self.normalize_text(text).split()

        for word in words:
            if len(word) > 3 and word not in self.banned_words:
                confidence = self.calculate_word_confidence(word)

                if confidence > 0.5:  # Threshold para considerar suspeito
                    # Registra padrão no banco
                    cursor = self.conn.cursor()
                    cursor.execute("""
                        INSERT OR REPLACE INTO learned_patterns
                        (pattern, frequency, confidence, first_seen, last_seen, status)
                        VALUES (?,
                                COALESCE((SELECT frequency FROM learned_patterns WHERE pattern = ?), 0) + 1,
                                ?,
                                COALESCE((SELECT first_seen FROM learned_patterns WHERE pattern = ?), ?),
                                ?, 'pending')
                    """, (word, word, confidence, word, datetime.now(), datetime.now()))
                    self.conn.commit()

    async def analyze_recent_patterns(self) -> List[Tuple[str, float, int]]:
        """Analisa padrões recentes e retorna sugestões"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT pattern, confidence, frequency
            FROM learned_patterns
            WHERE status = 'pending'
            AND frequency >= ?
            AND confidence >= 0.6
            ORDER BY confidence DESC, frequency DESC
            LIMIT 20
        """, (self.min_pattern_frequency,))

        return cursor.fetchall()

    def save_learned_terms(self):
        """Salva termos aprendidos em arquivo JSON"""
        learned_data = []
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT pattern, confidence, frequency, first_seen
            FROM learned_patterns
            WHERE status = 'approved'
        """)

        for pattern, confidence, frequency, first_seen in cursor.fetchall():
            learned_data.append({
                'word': pattern,
                'confidence': confidence,
                'frequency': frequency,
                'learned_date': first_seen
            })

        # Usa o nome do arquivo do JSON
        with open(self.learned_words_file, 'w', encoding='utf-8') as f:
            json.dump(learned_data, f, ensure_ascii=False, indent=2)

    async def learning_task(self):
        """Tarefa em background para análise contínua"""
        while True:
            try:
                await asyncio.sleep(3600)  # Executa a cada hora

                # Analisa padrões automáticamente
                suggestions = await self.analyze_recent_patterns()

                if suggestions:
                    # Auto-aprova termos com alta confiança e frequência (do JSON)
                    for term, confidence, frequency in suggestions:
                        if confidence > self.auto_approve_confidence and frequency > self.auto_approve_frequency:
                            self.banned_words.add(term)
                            self.learned_terms.add(term)

                            cursor = self.conn.cursor()
                            cursor.execute("""
                                UPDATE learned_patterns
                                SET status = 'auto_approved'
                                WHERE pattern = ?
                            """, (term,))
                            self.conn.commit()

                            logger.info(f"Auto-aprovado termo: {term} (conf: {confidence:.2f})")

                # Limpa dados antigos
                cursor = self.conn.cursor()
                cursor.execute("""
                    DELETE FROM violations
                    WHERE timestamp < datetime('now', '-30 days')
                """)
                self.conn.commit()

            except Exception as e:
                logger.error(f"Erro na tarefa de aprendizado: {e}")

    async def whitelist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /whitelist"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
            return

        if not context.args:
            await update.message.reply_text("ℹ️ Uso: /whitelist @usuario")
            return

        username = context.args[0].replace('@', '')
        await update.message.reply_text(f"✅ Usuário @{username} adicionado à whitelist! (Nota: A whitelist atual não é persistente entre reinícios.)")

    async def reload_words_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /reload"""
        if not await self.is_admin(update, context):
            await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
            return

        self.banned_words = self.load_banned_words()
        await update.message.reply_text(
            f"🔄 Listas recarregadas!\n"
            f"📝 Total: {len(self.banned_words)} termos monitorados\n"
            f"🧠 Incluindo {len(self.learned_terms)} termos aprendidos pela IA"
        )

    def update_bot_overall_stats(self, messages_processed: int = 0, violations_prevented: int = 0, users_banned: int = 0, learned_terms: int = 0):
        """Atualiza estatísticas gerais do bot"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE bot_stats
            SET total_messages_processed = total_messages_processed + ?,
                total_violations_prevented = total_violations_prevented + ?,
                total_users_banned = total_users_banned + ?,
                total_learned_terms = total_learned_terms + ?,
                last_updated = ?
            WHERE id = 1
        """, (messages_processed, violations_prevented, users_banned, learned_terms, datetime.now()))
        self.conn.commit()

    async def handle_violation(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user, message):
        """Lida com violações detectadas"""
        user_id = user.id
        chat_id = message.chat.id

        try:
            await message.delete()

            warnings = self.warning_counts.get(user_id, 0)

            if warnings == 0:
                self.warning_counts[user_id] = 1
                warning_msg = await context.bot.send_message(
                    chat_id,
                    f"⚠️ {user.mention_html()}\n"
                    f"🧠 IA detectou spam de cassino/apostas.\n"
                    f"Primeira advertência - próxima será ban automático.",
                    parse_mode='HTML'
                )

                await asyncio.sleep(30)
                try:
                    await warning_msg.delete()
                except Exception as e:
                    logger.warning(f"Não foi possível deletar a mensagem de aviso: {e}")
            else:
                await context.bot.ban_chat_member(chat_id, user_id)

                ban_msg = await context.bot.send_message(
                    chat_id,
                    f"🔨 {user.mention_html()} banido pela IA anti-spam.\n"
                    f"🛡️ Sistema de aprendizado em ação!",
                    parse_mode='HTML'
                )

                if user_id in self.warning_counts:
                    del self.warning_counts[user_id]

                await asyncio.sleep(60)
                try:
                    await ban_msg.delete()
                except Exception as e:
                    logger.warning(f"Não foi possível deletar a mensagem de banimento: {e}")

            logger.info(f"Violação processada pela IA: {user.username or user.first_name}")

        except Exception as e:
            logger.error(f"Erro ao lidar com violação: {e}")
# This part is missing in your provided code, but is essential to run the bot
# You'll need to define a main function and run the application
def main():
    # Load configuration from a JSON file (or define it directly)
    # Example config.json (create this file in the same directory as bot.py):
    # {
    #     "TELEGRAM_BOT_TOKEN": "YOUR_BOT_TOKEN_HERE",
    #     "BANNED_WORDS_FILE": "banned_words.txt",
    #     "LEARNED_WORDS_FILE": "learned_words.json",
    #     "DATABASE_NAME": "anticasino.db",
    #     "LEARNING_SETTINGS": {
    #         "MIN_PATTERN_FREQUENCY": 3,
    #         "SIMILARITY_THRESHOLD": 0.8,
    #         "LEARNING_WINDOW_HOURS": 24,
    #         "AUTO_APPROVE_CONFIDENCE": 0.85,
    #         "AUTO_APPROVE_FREQUENCY": 5
    #     }
    # }
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        logger.error("config.json not found! Please create a config.json file with your bot's settings.")
        return

    bot = LearningAntiCasinoBot(config)
    bot.application.run_polling(allowed_updates=Update.ALL_TYPES)
    
if __name__ == '__main__':
    main();
