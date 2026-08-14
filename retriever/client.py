import socket
import os
import sys

#binary I/O helper functions (used in LIST/GET/PUT)
from retriever import protocol as H




#the core implementations

def do_list(sock):
    """ask for directory listing over sock"""
    H.send_u8(sock,0) #send request code 0=list in my binary protocol
    response = H.recv_u8(sock) #reads status code from server (0-OK, 1-bad request, 2-internal error)
    length = H.recv_u64(sock) #reads 8 bytes u64 (how many bytes of payload coming next)
    #if length is >0 read exactly that many bytes from socket in data, else data is empty binary
    data = H.read_exact_bytes(sock,length) if length > 0 else b"" 

    peer= sock.getpeername()
    #if server said status not ok report failure and stop
    if response !=0:
        print("LIST FAILED")
        return
    
    #if there is payload, print a header and then each filename on its own line
    if data:
        print("Server Directory:")
        for name in data.split(b"\0"):
            if name:
                print(" -", name.decode("utf-8"))
    print("LIST complete")
    print(f"{peer[0]}:{peer[1]} | LIST | status=ok")

def do_get(sock,filename):
    """function that requests a file from server and saves it locally with the same name"""
    filename_bytes = filename.encode("utf-8")

    H.send_u8(sock,1) #send request code 1 as single byte (1 is get)
    H.send_u16(sock, len(filename_bytes)) #send a 2 byte length for filename (tells server how many bytes to read for next name)
    sock.sendall(filename_bytes) #send the actual filename bytes

    response = H.recv_u8(sock) #reads server status
    size = H.recv_u64(sock) #reads file size
    peer = sock.getpeername()

    #responce == 0 -- > OK . 1,2 ---> bad name, not found, error etc
    if response !=0:
        print("GET failed for {} .".format(filename) )
        peer = sock.getpeername()
        print(f"{peer[0]}:{peer[1]} | GET | {filename} | status=fail")
        return
    
    #prevent over-writing
    dest =filename
    if os.path.exists(dest):
        dest = f"downloaded_{os.path.basename(filename)}"
        print(f"Local '{filename}' exists; saving as '{dest}' instead.")

        

    try:
        #create a local file with same name 
        with open(dest, "wb") as f:
            remaining = size 
            while remaining > 0:
                chunk = sock.recv(min(65536,remaining))
                if not chunk:
                    raise ConnectionError("Connection lost during GET.")
                f.write(chunk)
                remaining -= len(chunk)

        print("Downloaded '{}' successfully ({} bytes).".format(dest,size))
        print(f"{peer[0]}:{peer[1]} | GET | {filename} | status=ok")
        print(f"Saved as '{dest}' in current folder.")

    except Exception as e:
        print("GET failed: {}".format(e))
        peer= sock.getpeername()
        print("{}:{} | GET | {} | status=fail".format(peer[0],peer[1],filename))
        print(f"{peer[0]}:{peer[1]} | GET | {filename} | status=fail")


def do_put(sock,filename, new_name=None):
    """a function that uploads file to server"""

    #get the servers ip address
    peer = sock.getpeername()

    #validate the file
    if not os.path.exists(filename):
        print("File '{}' not found".format(filename))
        return
    #checks if it is not an image
    if not H.is_image(filename):
        print("PUT only works with .jpg/.jpeg/.png")
        return
    
    #filesize in bytes (header)
    file_size = os.path.getsize(filename)
    
    remote = new_name or os.path.basename(filename)
    #encode filename from bytes to string
    filename_bytes = remote.encode("utf-8")
    #send request code 2 = put
    H.send_u8(sock,2)
    #send 2 bytes telling the server how many bytes are coming
    H.send_u16(sock,len(filename_bytes))
    #send the filename bytes raw
    sock.sendall(filename_bytes)
    #send 8 byte filesize so the server knows how many data bytes to read
    H.send_u64(sock,file_size)

    

    with open (filename,"rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sock.sendall(chunk)


    response = H.recv_u8(sock)
    

    if response ==0:
        print("Uploaded '{}' successfully ({}) bytes".format(filename,file_size))
        print(f"{peer[0]}:{peer[1]} | PUT | {filename} | status=ok")
    else:
        print("PUT failed for '{}' ".format(filename))
        print(f"{peer[0]}:{peer[1]} | PUT | {filename} | status=fail")



#creating a tcp client socket and connecting to the server
def create_client_socket(server_ip, port):
    cli_sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    #creating the client socket (TCP snd IPv4)
    print("Connecting |  host = {} | port = {}".format(server_ip,port))
    #connecting the client to the server IP and Port
    cli_sock.connect((server_ip,port))
    print("Connected | {}: {}".format(server_ip,port))
    #once connected, return the clients socket
    return cli_sock


def main():
    if len(sys.argv) < 4:
        print(f"Usage: python3 {os.path.basename(sys.argv[0])} <server_ip> <port> <list|get|put> [filename]")
        sys.exit(1)
    server_ip = sys.argv[1]

    try:
        port=int(sys.argv[2])
    except ValueError:
        print("Error: Port must be an integer.")
        sys.exit(1)
    if not (1024 <= port <=65535):
        print("Error: port must be between 1024 and 65535")
        sys.exit(1)
    
    command = sys.argv[3].lower()

    if command == "list":
        if len(sys.argv) !=4:
            print("Usage: python3 {} <server_ip> <port> list".format(os.path.basename(sys.argv[0])))
            sys.exit(1)
            
        filename = None
        new_name = None

    elif command == "get":
        if len(sys.argv)!=5:
            print("Usage: python3 {} <server_ip> <port> {} <filename>".format(os.path.basename(sys.argv[0]),command))
            sys.exit(1)
        filename = sys.argv[4]
        new_name = None

    elif command == "put":
        if len(sys.argv) not in (5, 6):
            print(f"Usage: python3 {os.path.basename(sys.argv[0])} <server_ip> <port> put <local_file> [new_name]")
            sys.exit(1)
        filename = sys.argv[4]
        new_name = sys.argv[5] if len(sys.argv) == 6 else None

    else:
        print("Invalid command. Use list, get, or put.")
        sys.exit(1)

    sock = create_client_socket(server_ip, port)
    try:
        if command == "list":
            do_list(sock)

        elif command == "get":
            do_get(sock,filename)

        elif command == "put":
            do_put(sock,filename,new_name)

    finally:
        try:
            sock.close()
        except Exception:
            pass
        print("Connection closed")
    

if __name__ == "__main__":
    main()

    