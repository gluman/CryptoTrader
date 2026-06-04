#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')

from src.core.config import Config

config = Config.load()
print('Config loaded OK!')
print('RAGFlow URL:', config.ragflow.get('base_url'))
print('Dataset ID:', config.ragflow.get('dataset_id'))
print('PostgreSQL Host:', config.postgresql.get('host'))