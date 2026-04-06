#!/bin/bash
docker exec docker-mysql-1 mysql -uroot -pGlumovRAG2024 -e "USE rag_flow; SELECT id, name, email FROM user LIMIT 10;"