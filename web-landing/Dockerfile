# Sunday landing — static site served by nginx.
FROM nginx:alpine

# Drop the default site and copy ours in.
RUN rm -rf /usr/share/nginx/html/*
COPY index.html styles.css app.js /usr/share/nginx/html/

# nginx:alpine already listens on port 80 with a sensible static config.
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
