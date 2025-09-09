from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import CheckConstraint

db = SQLAlchemy()

class PCBProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    remarks = db.Column(db.Text, default='') 
    tests = db.Column(db.Text, nullable=False)


class TestResult(db.Model):
    __tablename__ = 'test_results'

    id = db.Column(db.Integer, primary_key=True)
    profile_name = db.Column(db.String(120), nullable=False)
    results = db.Column(db.Text, nullable=False)
    graphs = db.Column(db.Text)  # Store graphs as JSON
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)  
