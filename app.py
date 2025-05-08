import os
import json
import time
import pika
import jwt
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_cors import CORS
from werkzeug.exceptions import BadRequest, Unauthorized, NotFound
import redis
from time import sleep
app = Flask(__name__)
CORS(app)


import logging
import sys
import json

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, self.datefmt),
        })

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
handler.setLevel(logging.INFO)

app.logger.handlers = [handler]
app.logger.setLevel(logging.INFO)

file_handler = logging.FileHandler('/var/log/catalog.log')
file_handler.setFormatter(JsonFormatter())
file_handler.setLevel(logging.INFO)
app.logger.addHandler(file_handler)


# Database configuration
# app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5434/client_db')
uri = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5434/client_db')
if uri.startswith('postgres://'):
    uri = uri.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# RabbitMQ configuration
RABBITMQ_URL = os.environ.get('MESSAGE_QUEUE_URL', 'amqp://guest:guest@localhost:5672')
EXCHANGE_NAME = 'product_events'

# JWT configuration
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'your-secret-key')


redis_host = os.getenv("REDIS_HOST", "localhost")
redis_port = int(os.getenv("REDIS_PORT", 6379))

cache = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)


# Models
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    image = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'price': self.price,
            'quantity': self.quantity,
            'image': self.image,
            'createdAt': self.created_at.isoformat(),
            'updatedAt': self.updated_at.isoformat()
        }

class ProductLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.UniqueConstraint('user_id', 'product_id', name='unique_user_product'),
    )

# class Purchase(db.Model):
#     id = db.Column(db.Integer, primary_key=True)
#     user_id = db.Column(db.Integer, nullable=False)
#     product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
#     purchase_date = db.Column(db.DateTime, default=datetime.utcnow)
    
#     product = db.relationship('Product', backref='purchases')
    
#     def to_dict(self):
#         return {
#             'id': self.id,
#             'userId': self.user_id,
#             'productId': self.product_id,
#             'purchaseDate': self.purchase_date.isoformat(),
#             'product': self.product.to_dict() if self.product else None
#         }

# Message queue functions
def publish_message(routing_key, message):
    try:
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        channel = connection.channel()
        
        channel.exchange_declare(exchange=EXCHANGE_NAME, exchange_type='topic', durable=True)
        
        channel.basic_publish(
            exchange=EXCHANGE_NAME,
            routing_key=routing_key,
            body=json.dumps(message),
            properties=pika.BasicProperties(
                delivery_mode=2,  # make message persistent
            )
        )
        
        connection.close()
        return True
    except Exception as e:
        app.logger.error(f"Error publishing message: {str(e)}")
        return False

# Message consumer
def start_consumer():
    try:
        connection = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
        channel = connection.channel()
        
        # Declare both exchanges
        channel.exchange_declare(exchange='product_events', exchange_type='topic', durable=True)
        channel.exchange_declare(exchange='order_events', exchange_type='topic', durable=True)

        # Create a queue for this service
        result = channel.queue_declare(queue='client_service_queue', durable=True)
        queue_name = result.method.queue
        
        # Bind to product lifecycle events
        channel.queue_bind(exchange='product_events', queue=queue_name, routing_key='product.*')
        
        # Bind to purchase events (new requirement)
        channel.queue_bind(exchange='product_events', queue=queue_name, routing_key='purchase.created')

        # (Optional) Bind to order events if needed later
        channel.queue_bind(exchange='order_events', queue=queue_name, routing_key='order.*')
        
        def callback(ch, method, properties, body):
            try:
                with app.app_context():
                    data = json.loads(body)
                    routing_key = method.routing_key

                    if routing_key == 'product.created':
                        product = Product(
                            id=data['id'],
                            name=data['name'],
                            description=data['description'],
                            price=data['price'],
                            quantity=data['quantity'],
                            image=data.get('image', '')
                        )
                        db.session.add(product)
                        db.session.commit()
                        app.logger.info(f"Created product: {product.id}")

                        cache.delete('products:all')
                        app.logger.info(f"Deleted cache for products")

                    elif routing_key == 'product.updated':
                        product = Product.query.get(data['id'])
                        if product:
                            product.name = data['name']
                            product.description = data['description']
                            product.price = data['price']
                            product.quantity = data['quantity']
                            product.image = data.get('image', '')
                            db.session.commit()
                            app.logger.info(f"Updated product: {product.id}")
                        else:
                            product = Product(
                                id=data['id'],
                                name=data['name'],
                                description=data['description'],
                                price=data['price'],
                                quantity=data['quantity'],
                                image=data.get('image', '')
                            )
                            db.session.add(product)
                            db.session.commit()
                            app.logger.info(f"Created missing product: {product.id}")

                        cache.delete(f"product:{product.id}")

                    elif routing_key == 'product.deleted':
                        product = Product.query.get(data['id'])
                        if product:
                            db.session.delete(product)
                            db.session.commit()
                            app.logger.info(f"Deleted product: {data['id']}")

                        cache.delete(f"product:{product.id}")

                    elif routing_key == 'purchase.created':
                        items = data.get('data', [])
                        for item in items:  # data is a list of items
                            product_id = item.get('productId')
                            quantity = item.get('quantity', 1)

                            if product_id:
                                product = Product.query.get(product_id)
                                if product:
                                    if product.quantity >= quantity:
                                        product.quantity -= quantity
                                        db.session.commit()
                                        app.logger.info(f"Reduced quantity for product {product_id} by {quantity}. New quantity: {product.quantity}")
                                    else:
                                        app.logger.warning(f"Insufficient quantity for product {product_id}")
                                else:
                                    app.logger.error(f"Product {product_id} not found")
                            cache.delete(f"product:{product_id}")
                
                ch.basic_ack(delivery_tag=method.delivery_tag)
                

            except Exception as e:
                app.logger.error(f"Error processing message: {str(e)}")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(queue=queue_name, on_message_callback=callback)
        
        app.logger.info('Started consuming messages from queue')
        channel.start_consuming()
            
    except Exception as e:
        app.logger.error(f"RabbitMQ consumer error: {str(e)}")
        # Retry after delay
        time.sleep(5)
        start_consumer()


# Authentication middleware
def get_user_from_token():
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    
    token = auth_header.split(' ')[1]
    
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
        app.logger.info(f"Decoded JWT payload: {payload}")
        return {
            'id': payload.get('user_id'),
            'is_staff': payload.get('is_staff', False)
        }
    except:
        app.logger.error("Invalid token")
        return None

def token_required(f):
    def decorator(*args, **kwargs):
        user = get_user_from_token()
        app.logger.info(f"User from token: {user}")
        if not user:
            app.logger.warning("Unauthorized access attempt")
            raise Unauthorized('Authentication required')
            
        # Add user to request context
        request.user = user
        return f(*args, **kwargs)
    
    decorator.__name__ = f.__name__
    return decorator

# Routes
@app.route('/catalog-api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'service': 'product-catalog-service'}), 200

@app.route('/catalog-api/products', methods=['GET'])
def get_products():
    cache_key = 'products:all'
    # Try to get from cache
    cached_data = cache.get(cache_key)
    if cached_data:
        app.logger.info("Serving products from cache")
        return jsonify(json.loads(cached_data))

    # Fallback to DB
    products = Product.query.all()
    product_list = [product.to_dict() for product in products]

    # Store in cache
    cache.setex(cache_key, 300, json.dumps(product_list))  # Cache for 5 minutes

    app.logger.info(f"Fetched {len(products)} products from DB and cached")
    return jsonify(product_list)

@app.route('/catalog-api/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    cache_key = f"product:{product_id}"
    app.logger.info(f"Looking for cache key: {cache_key}")

    # Try to get cached data
    cached_data = cache.get(cache_key)
    if cached_data:
        app.logger.info(f"Cache hit for product {product_id}")
        return jsonify(json.loads(cached_data))

    # If not cached, query from DB
    product = Product.query.get_or_404(product_id)
    product_dict = product.to_dict()

    # Store in cache (e.g., for 5 minutes)
    cache.setex(cache_key, 300, json.dumps(product_dict))
    app.logger.info(f"Fetched product {product_id} from DB and cached")

    return jsonify(product_dict)

@app.route('/catalog-api/products/<int:product_id>/like', methods=['POST'])
@token_required
def toggle_like_product(product_id):
    # user_id = request.headers.get('X-User-Id')  # Assuming user ID is passed in headers
    user_id = request.user['id']
    if not user_id:
        app.logger.warning("User ID is required for liking/unliking a product")
        return jsonify({'error': 'User ID is required'}), 400

    # Check if the product is already liked
    existing_like = ProductLike.query.filter_by(user_id=user_id, product_id=product_id).first()

    if existing_like:
        # If already liked, remove the like (unlike)
        db.session.delete(existing_like)
        db.session.commit()
        app.logger.info(f"Product {product_id} unliked by user {user_id}")
        return jsonify({'message': 'Product unliked successfully'}), 200

    # If not liked, add a new like
    new_like = ProductLike(user_id=user_id, product_id=product_id)
    db.session.add(new_like)
    db.session.commit()
    app.logger.info(f"Product {product_id} liked by user {user_id}")
    cache.delete(f"product:{product_id}")
    return jsonify({'message': 'Product liked successfully'}), 201

@app.route('/catalog-api/products/<int:product_id>/is-liked', methods=['GET'])
@token_required
def is_product_liked(product_id):
    user_id = request.user['id']
    if not user_id:
        app.logger.warning("User ID is required to check if product is liked")
        return jsonify({'error': 'User ID is required'}), 400

    # Check if the product is liked by the user
    existing_like = ProductLike.query.filter_by(user_id=user_id, product_id=product_id).first()
    return jsonify({'isLiked': bool(existing_like)}), 200

@app.route('/catalog-api/products/liked', methods=['GET'])
@token_required
def get_liked_products():
    user_id = request.user['id']
    if not user_id:
        app.logger.warning("User ID is required to fetch liked products")
        return jsonify({'error': 'User ID is required'}), 400

    # Retrieve all liked products for the user
    liked_products = ProductLike.query.filter_by(user_id=user_id).all()
    product_ids = [like.product_id for like in liked_products]

    # Fetch full product details for the liked products
    products = Product.query.filter(Product.id.in_(product_ids)).all()
    product_details = [product.to_dict() for product in products]
    app.logger.info(f"Fetched {len(product_details)} liked products for user {user_id}")
    return jsonify({'likedProducts': product_details}), 200

# @app.route('/catalog-api/products/<int:product_id>/purchase', methods=['POST'])
# @token_required
# def purchase_product(product_id):
#     product = Product.query.get_or_404(product_id)
#     user_id = request.user['id']
    
#     # Check if product is available
#     if product.quantity <= 0:
#         app.logger.warning(f"Product {product_id} is out of stock")
#         raise BadRequest('Product is out of stock')
    
#     # Create purchase record
#     purchase = Purchase(user_id=user_id, product_id=product_id)
#     db.session.add(purchase)
    
#     # Update product quantity in client database
#     product.quantity -= 1
#     db.session.commit()
#     app.logger.info(f"Product {product_id} purchased by user {user_id}. Remaining quantity: {product.quantity}")
    
#     # Publish message to update admin service
#     publish_message('purchase.created', {
#         'productId': product_id,
#         'userId': user_id,
#         'purchaseId': purchase.id,
#         'purchaseDate': purchase.purchase_date.isoformat()
#     })
    
#     return jsonify(purchase.to_dict()), 201

# @app.route('/catalog-api/purchases', methods=['GET'])
# @token_required
# def get_purchases():
#     user_id = request.user['id']
#     purchases = Purchase.query.filter_by(user_id=user_id).all()
#     app.logger.info(f"Fetched {len(purchases)} purchases for user {user_id}")
#     return jsonify([purchase.to_dict() for purchase in purchases])

# Error handlers
@app.errorhandler(BadRequest)
def handle_bad_request(e):
    app.logger.error(f"Bad request: {str(e)}")
    return jsonify({'error': str(e)}), 400

@app.errorhandler(Unauthorized)
def handle_unauthorized(e):
    app.logger.error(f"Unauthorized access: {str(e)}")
    return jsonify({'error': str(e)}), 401

@app.errorhandler(NotFound)
def handle_not_found(e):
    app.logger.error(f"Resource not found: {str(e)}")
    return jsonify({'error': str(e)}), 404

@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error(f"Unhandled exception: {str(e)}")
    return jsonify({'error': 'Internal server error'}), 500

app.logger.info("Starting consumer thread...")
threading.Thread(target=start_consumer, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
