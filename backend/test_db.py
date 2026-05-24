from sqlalchemy import create_engine

engine = create_engine("postgresql://postgres:admin@localhost:5432/saas_disparador")

conn = engine.connect()
print("conectou")   