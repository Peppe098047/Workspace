# Immagine base leggera: nginx su Alpine (~10 MB)
FROM nginx:alpine

# Copia il file di testo nella document root di nginx
COPY hello.txt /usr/share/nginx/html/hello.txt

# Porta esposta dal server nginx all'interno del container
EXPOSE 80

# nginx:alpine ha già il CMD corretto, non serve ridefinirlo
