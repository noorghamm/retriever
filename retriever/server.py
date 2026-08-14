import sys
import os
import socket

#binary I/O helper functions (used in LIST/GET/PUT)
from retriever import protocol as H

#function that creates a tcp socket and binds it to an ip port
def create_server_socket(port):
     #creates a server socket (AF_INET) indicates that its in IPV4, SOCK_STREAM shows that its a TCP socket (SOCK_DGRAM) for UDP
    srv_sock= socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        # binds the socket to specific IP and port
        #"0.0.0.0" is the deafult to bind to ANY IP address

         #This prevents the "Address already in use" error when restarting the server.
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("0.0.0.0",port))  

        #listen for incoming connections, with a size of queue that is set to 5
        srv_sock.listen(5)
        print("Server is ready on 0.0.0.0, running on port {}".format(port))
    except OSError as e:
        print("ERROR binding Socket on port {}:{}".format(port, e))

        #exit with 1 indicating an error
        sys.exit(1)
    return srv_sock

def handle_client(cli_sock,cli_addr):
    """
    Binary protocol:
      Client -> Server:
        LIST: [u8:0]
        GET : [u8:1][u16:filename_length][fname_bytes]
        PUT : [u8:2][u16:filename_length][fname_bytes][u64:file_size][file_bytes]
    """
    try:
        #read the first unsigned integer code, 0=LIST, 1=GET, 2=PUT
        code = H.recv_u8(cli_sock)

        if code == 0:  #LIST
            handle_list(cli_sock, cli_addr)
            return
        
        elif code == 1: #GET
            #client sends a 2 byte number(length of file name) to the server 
            file_name_len = H.recv_u16(cli_sock)
            #calls helper function to read until it has file_name_len bytes. 
            file_name = H.read_exact_bytes(cli_sock,file_name_len).decode("utf-8") #filename bytes are converted to a python string
            handle_get(cli_sock,cli_addr,file_name)
            return
        elif code == 2: #PUT
            file_name_len = H.recv_u16(cli_sock)
            file_name = H.read_exact_bytes(cli_sock,file_name_len).decode("utf-8")
            file_size = H.recv_u64(cli_sock)
            handle_put(cli_sock,cli_addr,file_name,file_size)
            return
        else:
            H.send_u8(cli_sock,1)
            H.send_u64(cli_sock,0)
            print(" {}:{} | UNKNOWN({}) | bad code".format(cli_addr[0],cli_addr[1],code))

    except (ConnectionError, ConnectionResetError) as e:
        print(f"{cli_addr[0]}:{cli_addr[1]} | CLIENT_DISCONNECT | {e}")
    except Exception as e:
        try:
            H.send_u8(cli_sock,2)
            H.send_u64(cli_sock,0)
        except Exception as e:
            pass
        print("{}:{} |INTERNAL | {}".format(cli_addr[0],cli_addr[1],e))
    finally:
        cli_sock.close()
        print(f"{cli_addr[0]}:{cli_addr[1]} | DISCONNECTED")

def handle_list(cli_sock,cli_addr):
    try:
        #list all files and folders in directory
        names = os.listdir(".")
        #encode converts each filename (string) into bytes, join null byte is used to seperate them
        payload = b"\0".join(n.encode("utf-8") for n in names)
        H.send_u8(cli_sock,0)
        H.send_u64(cli_sock,len(payload))
        if payload:
            cli_sock.sendall(payload)
        print("{}:{} | LIST | status=ok".format(cli_addr[0],cli_addr[1]))
    except Exception as e:
        try:
            H.send_u8(cli_sock,2)
            H.send_u64(cli_sock,0)
        except Exception:
            pass
        print("{}:{} | LIST | status=fail {}".format(cli_addr[0],cli_addr[1],e))


def handle_get(cli_sock,cli_addr,filename):
    try:
        if not H.check_filename(filename):
            H.send_u8(cli_sock,1)
            H.send_u64(cli_sock,0)
            print("{}:{} | GET {} | status=fail:bad_name".format(cli_addr[0],cli_addr[1],filename))
            return
        
        if not os.path.exists(filename) or not os.path.isfile(filename):
            H.send_u8(cli_sock,1)
            H.send_u64(cli_sock,0)
            print("{}:{} | GET |{}| status=fail:not_found".format(cli_addr[0],cli_addr[1],filename))
            return
        
        size = os.path.getsize(filename)
        H.send_u8(cli_sock,0)
        H.send_u64(cli_sock,size)

        with open(filename, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                cli_sock.sendall(chunk)
        print("{}:{}|GET|{}| status=ok".format(cli_addr[0],cli_addr[1],filename))
    except Exception as e:
        try:
            H.send_u8(cli_sock,2)
            H.send_u64(cli_sock,0)
        except Exception:
            pass
        print("{}:{}|GET | {} | status = fail: {}".format(cli_addr[0], cli_addr[1],filename,e))

def handle_put(cli_sock, cli_addr, filename, file_size):
    try:
        #validate file name
        if not H.check_filename(filename):
            H.send_u8(cli_sock,1)
            print("{}:{}| PUT | {} satus=fail:bad_name".format(cli_addr[0], cli_addr[1], filename))
            return
            
        #checks if it is an image
        
        if not H.is_image(filename):
            H.send_u8(cli_sock,1)
            print("{}:{}| PUT | {} | status=fail:not_image".format(cli_addr[0],cli_addr[1],filename))
            return
        need = min(8,file_size)
        head = H.read_exact_bytes(cli_sock,need)
        
        if not (H.is_jpeg_header(head) or H.is_png_header(head)):
            H.send_u8(cli_sock,1)
            print("{}:{} | PUT |{}| status=fail:not_image".format(cli_addr[0],cli_addr[1],filename))
            return


        try:
            out = open(filename,"xb")
        except FileExistsError:
            H.send_u8(cli_sock,1)
            print("{}:{} | PUT| {} | status=fail:file exist".format(cli_addr[0],cli_addr[1],filename))
            return
        written =0
        with out:
            out.write(head)
            written += len(head)
            while written < file_size :
                need = min(65536, file_size - written)
                chunk = H.read_exact_bytes(cli_sock, need)
                out.write(chunk)
                written +=len(chunk)

        H.send_u8(cli_sock,0)
        print("{}:{} | PUT |{} | OK: {}B".format(cli_addr[0],cli_addr[1],filename,written))

    except Exception as e:
        try:
            if 'out' in locals() and not out.closed:
                out.close()
            if os.path.exists(filename):
                os.remove(filename)
        except Exception as e:
            print(f"Cleanup failed: {cleanup_e}")
            
        try:
            H.send_u8(cli_sock,2)
        except Exception:
            pass
        print("{}:{}| PUT | {} | status=fail: {}".format(cli_addr[0],cli_addr[1],filename,e))



      
def start_server(port):
    """The main server function: accept and process client connections."""
    srv_sock = create_server_socket(port)
    try:
        while True:
            try:
                # accept() takes the first request from the queue and processes it.
                # If there is no request, wait until a new client connects.
                cli_sock, cli_addr = srv_sock.accept()

                # cli_addr is a tuple (IP, port)
                print("{}:{} | CONNECTED".format(cli_addr[0], cli_addr[1]))

                # Handle the client (this function will be implemented later)
                handle_client(cli_sock, cli_addr)

            except Exception as e:
                # Catch any errors with one simple message
                print("Server error:", e)

    except KeyboardInterrupt:
        print("Server down: Keyboard Interruption")

    finally:
        srv_sock.close()
        print("Server socket closed")
if __name__ == "__main__":
    if len(sys.argv) !=2:
        print("Usage: python3 server.py <port>")
        #exit program with error code
        sys.exit(1)
    try:
        port = int(sys.argv[1])
        if not (1024 <= port <= 65535):
            print("port must be between 1024 and 65535")
            sys.exit(1)
    except ValueError:
        print("Invalid port number. Please print a valid integer for the port")
        sys.exit(1)

    start_server(port)



