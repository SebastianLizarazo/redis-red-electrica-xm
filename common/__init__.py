# Dominio compartido entre publisher, subscriber, api y dashboard.
# Aquí viven modelos Pydantic, constantes de keys Redis, Protocol DataSource,
# configuración y logging. Cualquier módulo puede importar desde aquí
# sin riesgo de ciclos porque no depende de los otros paquetes.
