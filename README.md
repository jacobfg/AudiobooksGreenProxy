[English](/README_en.md) 

# AudiobooksGreenProxy

Это self hosted прокси для работы в связке с плеером аудиокниг [AudiobooksGreen](https://github.com/fabrikant/AudiobooksGreen).

# Способы установки и запуска

## Docker compose - из готового образа (рекомендуется)
Создать на вашем сервере каталог. Создать в нем файл **docker-compose.yml** с содержимым из файла [docker-compose.yml.example](https://github.com/fabrikant/AudiobooksGreenProxy/blob/main/docker-compose.yml.example). Следуя рекомендациям из комментариев установить свои значения. 

Рядом с файлом **docker-compose.yml** создать каталог **data**

### Запуск
```
docker compose up -d
```

### Остановка
```
docker compose down
```

### Обновление
```
docker compose pull
docker compose down
docker compose up -d
docker image prune
```

# Сборка docker image из исходников

```
docker build -t audiobooks_green_proxy .
```

Запускать также как в предыдущем пункте. Только в файле **docker-compose.yml** нужно заменть имя image на то, которое у вас в системе.
Посмотреть имена image можно командой
```
docker image ls -a
```

# Запуск Python кода

### Установка и настройка виртуального окружения
```
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --upgrade -r requirements.txt
mkdir data
deactivate
```

### Запуск
Активация виртуального окружения
```
source .venv/bin/activate
```

Установка переменных окружения (если это требуется)
```
export SSL_CERT_FILE=./data/fullchain.pem
export SSL_PRIVATE_KEY_FILE=./data/privkey.pem
```

Запуск прокси
```
python3 books_proxy.py 
```


## Container releases

Image publication runs on `release: published` for stable releases. Branch and tag
pushes do not publish images.
