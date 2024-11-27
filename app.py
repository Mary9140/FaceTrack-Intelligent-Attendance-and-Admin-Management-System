from flask import Flask, render_template, request
import boto3
import cv2
import os
from datetime import datetime
from werkzeug.utils import secure_filename
import uuid  # Ensure to import uuid for unique image naming

app = Flask(__name__)

# AWS and DynamoDB configurations
AWS_REGION = "us-east-1"
S3_BUCKET = "attendance001"
EMPLOYEE_IMAGES_FOLDER = "employee_faces/"
ATTENDANCE_LOGS_FOLDER = "attendance_logs/"
ADMIN_IMAGES_FOLDER = "admin_faces/"
REKOGNITION_CLIENT = boto3.client("rekognition", region_name=AWS_REGION)
DYNAMODB_RESOURCE = boto3.resource("dynamodb", region_name=AWS_REGION)
S3_RESOURCE = boto3.resource("s3", region_name=AWS_REGION)  # Use resource to access Bucket
S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)

# DynamoDB Tables
EMPLOYEES_TABLE = DYNAMODB_RESOURCE.Table("Employees")
ATTENDANCE_TABLE = DYNAMODB_RESOURCE.Table("Attendance")
ADMINS_TABLE = DYNAMODB_RESOURCE.Table("Admins")

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

@app.route("/capture", methods=["POST"])
def capture_image():
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        return "Unable to access the camera", 500

    ret, frame = cap.read()
    if not ret:
        return "Failed to capture an image", 500

    image_filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    cv2.imwrite(image_filename, frame)
    cap.release()

    s3_image_path = f"{ATTENDANCE_LOGS_FOLDER}{image_filename}"
    S3_CLIENT.upload_file(image_filename, S3_BUCKET, s3_image_path)

    os.remove(image_filename)

    employees = EMPLOYEES_TABLE.scan()["Items"]
    for employee in employees:
        source_image = {"S3Object": {"Bucket": S3_BUCKET, "Name": employee["ImagePath"]}}
        target_image = {"S3Object": {"Bucket": S3_BUCKET, "Name": s3_image_path}}

        # Debug: Print the S3 paths
        print(f"Comparing source image: {source_image}")
        print(f"Comparing target image: {target_image}")

        try:
            response = REKOGNITION_CLIENT.compare_faces(
                SourceImage=source_image,
                TargetImage=target_image,
                SimilarityThreshold=90,
            )

            if response["FaceMatches"]:
                ATTENDANCE_TABLE.put_item(
                    Item={
                        "EmployeeID": employee["EmployeeID"],
                        "Date": datetime.now().strftime("%Y-%m-%d"),
                        "Time": datetime.now().strftime("%H:%M:%S"),
                        "Status": "Present",
                    }
                )
                employee_image_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{employee['ImagePath']}"
                tasks = employee.get("Tasks", [])
                employee_name = employee.get("Name", "Unknown")
                employee_role = employee.get("Role", "Not Assigned")

                return render_template(
                    "attendance.html",
                    employee_image_url=employee_image_url,
                    tasks=tasks,
                    employee_name=employee_name,
                    employee_role=employee_role,
                )

        except Exception as e:
            print(f"Error during Rekognition comparison: {e}")
            return "Error during face comparison", 500

    return "Face not recognized", 404


@app.route("/admin", methods=["GET"])
def admin_login_page():
    return render_template("admin_login.html")

@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.html")

    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        return "Unable to access the camera", 500

    ret, frame = cap.read()
    if not ret:
        return "Failed to capture an image", 500

    image_filename = f"admin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    cv2.imwrite(image_filename, frame)
    cap.release()

    s3_image_path = f"admin_logs/{image_filename}"
    S3_CLIENT.upload_file(image_filename, S3_BUCKET, s3_image_path)

    os.remove(image_filename)

    admins = ADMINS_TABLE.scan()["Items"]
    for admin in admins:
        source_image = {"S3Object": {"Bucket": S3_BUCKET, "Name": admin["ImagePath"]}}
        target_image = {"S3Object": {"Bucket": S3_BUCKET, "Name": s3_image_path}}

        response = REKOGNITION_CLIENT.compare_faces(
            SourceImage=source_image,
            TargetImage=target_image,
            SimilarityThreshold=90,
        )

        if response["FaceMatches"]:
            return render_template("admin_dashboard.html", employees=get_all_employees())

    return "Admin not recognized", 403

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/admin_dashboard", methods=["GET", "POST"])
def admin_dashboard():
    if request.method == "POST":
        action = request.form.get("action")
        
        # Handle Add Employee
        if action == "add_employee":
            name = request.form.get("name")
            role = request.form.get("role")
            employee_image = request.files.get("employee_image")

            if employee_image and allowed_file(employee_image.filename):
                # Save the image to S3
                filename = secure_filename(employee_image.filename)
                image_key = str(uuid.uuid4()) + "." + filename.rsplit('.', 1)[1].lower()  # Unique filename
                s3_bucket = S3_RESOURCE.Bucket(S3_BUCKET)  # Using the resource to access Bucket
                s3_bucket.put_object(Key=image_key, Body=employee_image, ContentType=employee_image.content_type)

                # Get the image URL
                image_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{image_key}"

                # Add new employee to DynamoDB
                employee_id = str(uuid.uuid4())  # Generate a unique employee ID
                EMPLOYEES_TABLE.put_item(
                    Item={
                        "EmployeeID": employee_id,
                        "Name": name,
                        "Role": role,
                        "ImagePath": image_url,
                        "Tasks": []
                    }
                )

        # Handle Remove Employee
        elif action == "remove_employee":
            employee_id = request.form.get("employee_id")
            EMPLOYEES_TABLE.delete_item(Key={"EmployeeID": employee_id})

        # Handle Add Task
        elif action == "add_task":
            employee_id = request.form.get("employee_id")
            task = request.form.get("task")
            EMPLOYEES_TABLE.update_item(
                Key={"EmployeeID": employee_id},
                UpdateExpression="SET Tasks = list_append(Tasks, :task)",
                ExpressionAttributeValues={":task": [task]},
                ReturnValues="UPDATED_NEW"
            )

        # Handle Remove Task
        elif action == "remove_task":
            employee_id = request.form.get("employee_id")
            task = request.form.get("task")
            EMPLOYEES_TABLE.update_item(
                Key={"EmployeeID": employee_id},
                UpdateExpression="SET Tasks = list_remove(Tasks, :task)",
                ExpressionAttributeValues={":task": task},
                ReturnValues="UPDATED_NEW"
            )

    # Get the list of employees from DynamoDB
    employees = get_all_employees()
    return render_template("admin_dashboard.html", employees=employees)

def get_all_employees():
    employees = []
    response = EMPLOYEES_TABLE.scan()
    employees.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        response = EMPLOYEES_TABLE.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        employees.extend(response.get("Items", []))

    return employees

if __name__ == "__main__":
    app.run(debug=True)
